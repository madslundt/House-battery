"""Small, explainable policy adjustments around the core optimizer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from .models import Action, PlannerSettings, PriceSlot

_GRID_AVAILABLE = {"1", "on", "available", "grid", "on_grid", "connected", "true"}
_GRID_UNAVAILABLE = {"0", "off", "unavailable", "no_grid", "disconnected", "false"}


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    """Effective target and evidence for opportunistic extra storage."""

    target_soc: float
    price_spread_dkk_per_kwh: float
    effective_margin_dkk_per_kwh: float
    active: bool
    reason: str


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
        "Self-Gen/Zero Export": Action.BATTERY,
    }.get(value, Action.SAFE)


def apply_storage_policy(
    settings: PlannerSettings,
    slots: Iterable[PriceSlot],
    *,
    extra_storage_spread_dkk_per_kwh: float,
    opportunistic_target_soc: float,
) -> tuple[PlannerSettings, StoragePolicy]:
    """Lift the charge ceiling only when a large spread remains worthwhile."""
    prices = [slot.price for slot in slots]
    if not prices:
        return settings, StoragePolicy(
            settings.target_soc, 0, 0, False, "No valid price spread available"
        )
    low, high = min(prices), max(prices)
    spread = high - low
    effective_margin = (
        high
        - low / settings.round_trip_efficiency
        - settings.degradation_cost_dkk_per_kwh
    )
    active = (
        spread >= extra_storage_spread_dkk_per_kwh
        and effective_margin >= settings.minimum_profit_dkk_per_kwh
        and opportunistic_target_soc > settings.target_soc
    )
    if not active:
        return settings, StoragePolicy(
            settings.target_soc,
            spread,
            effective_margin,
            False,
            "Normal target retained; spread does not justify extra stored energy",
        )
    target = min(100.0, opportunistic_target_soc)
    return replace(settings, target_soc=target), StoragePolicy(
        target,
        spread,
        effective_margin,
        True,
        "Extra storage target enabled by a profitable known price spread",
    )
