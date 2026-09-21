"""Small, explainable policy adjustments around the core optimizer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from math import isfinite

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
    known_slot_count: int = 0
    conservative_charge_price_dkk_per_kwh: float | None = None
    conservative_discharge_price_dkk_per_kwh: float | None = None


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


def apply_storage_policy(
    settings: PlannerSettings,
    slots: Iterable[PriceSlot],
    *,
    extra_storage_spread_dkk_per_kwh: float,
    opportunistic_target_soc: float,
) -> tuple[PlannerSettings, StoragePolicy]:
    """Lift the charge ceiling only for a conservative known-price opportunity.

    Forecast prices may extend the optimizer's horizon, but they must never
    create permission to store above the normal target.  The low price is the
    conservative import price and the high price is the conservative avoided
    import price, including any uncertainty carried by a ``PriceSlot``.
    """
    known_slots = [
        slot
        for slot in slots
        if (
            slot.source == "known"
            and isfinite(slot.charge_price_dkk_per_kwh)
            and isfinite(slot.discharge_price_dkk_per_kwh)
        )
    ]
    if not known_slots:
        return settings, StoragePolicy(
            settings.target_soc,
            0,
            0,
            False,
            "Normal target retained; no valid known-price opportunity is available",
        )
    low = min(slot.charge_price_dkk_per_kwh for slot in known_slots)
    high = max(slot.discharge_price_dkk_per_kwh for slot in known_slots)
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
            "Normal target retained; conservative known-price margin does not "
            "justify extra stored energy",
            len(known_slots),
            low,
            high,
        )
    target = min(100.0, opportunistic_target_soc)
    return replace(settings, target_soc=target), StoragePolicy(
        target,
        spread,
        effective_margin,
        True,
        "Extra storage target eligible from a profitable conservative "
        "known-price opportunity",
        len(known_slots),
        low,
        high,
    )
