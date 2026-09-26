"""Small, explainable policy adjustments around the core optimizer."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Action, Plan, PlannerSettings

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


@dataclass(frozen=True, slots=True)
class OpportunisticPlanDecision:
    """Selected plan plus evidence for an optional higher charge ceiling."""

    plan: Plan
    settings: PlannerSettings
    active: bool
    reason: str
    incremental_savings_dkk: float


def apply_storage_policy(
    settings: PlannerSettings,
) -> PlannerSettings:
    """Keep the configured target as the normal hard charge ceiling."""
    return settings


def select_opportunistic_plan(
    normal_plan: Plan,
    opportunistic_plan: Plan,
    normal_settings: PlannerSettings,
    opportunistic_settings: PlannerSettings,
    *,
    enabled: bool,
) -> OpportunisticPlanDecision:
    """Use the higher-SOC plan only for a complete, known, profitable cycle.

    Merely assigning a terminal value to energy left at the horizon is not
    enough. The candidate must charge more than the normal plan, exceed the
    normal target on known prices, discharge more later on known prices, return
    to the normal target by the end of the horizon, and improve savings that do
    not include the notional terminal credit.
    """
    incremental_savings = (
        opportunistic_plan.realized_savings_dkk - normal_plan.realized_savings_dkk
    )

    def normal(reason: str) -> OpportunisticPlanDecision:
        return OpportunisticPlanDecision(
            plan=normal_plan,
            settings=normal_settings,
            active=False,
            reason=reason,
            incremental_savings_dkk=max(0.0, incremental_savings),
        )

    if not enabled:
        return normal("Disabled; using the normal charge target")
    if opportunistic_settings.target_soc <= normal_settings.target_soc:
        return normal("Opportunistic target is not above the normal target")
    if not opportunistic_plan.slots:
        return normal("No executable opportunistic plan")

    energy_tolerance_wh = opportunistic_settings.energy_step_wh / 2
    soc_tolerance = (
        100 * opportunistic_settings.energy_step_wh / opportunistic_settings.capacity_wh
    )
    normal_charge_wh = sum(slot.battery_charge_wh for slot in normal_plan.slots)
    extra_charge_wh = (
        sum(slot.battery_charge_wh for slot in opportunistic_plan.slots)
        - normal_charge_wh
    )
    peak_index, peak_slot = max(
        enumerate(opportunistic_plan.slots),
        key=lambda item: item[1].soc_end,
    )
    candidate_known_discharge_wh = sum(
        slot.battery_discharge_wh
        for slot in opportunistic_plan.slots[peak_index + 1 :]
        if slot.price_source == "known"
    )
    normal_known_discharge_wh = sum(
        slot.battery_discharge_wh
        for slot in normal_plan.slots
        if slot.start >= peak_slot.end and slot.price_source == "known"
    )
    extra_known_discharge_wh = (
        candidate_known_discharge_wh - normal_known_discharge_wh
    )
    last_known_slot = next(
        (
            slot
            for slot in reversed(opportunistic_plan.slots)
            if slot.price_source == "known"
        ),
        None,
    )
    returned_to_normal_target_on_known_prices = (
        last_known_slot is not None
        and last_known_slot.soc_end <= normal_settings.target_soc + soc_tolerance
    )

    if extra_charge_wh <= energy_tolerance_wh:
        return normal("Higher target does not add meaningful charging")
    if peak_slot.soc_end <= normal_settings.target_soc + soc_tolerance:
        return normal("Plan does not need storage above the normal target")
    if (
        peak_slot.price_source != "known"
        or extra_known_discharge_wh <= energy_tolerance_wh
    ):
        return normal("Extra capacity is not supported by a known-price charge/discharge cycle")
    if not returned_to_normal_target_on_known_prices:
        return normal("Extra energy is not scheduled for use within the known horizon")
    if opportunistic_plan.realized_savings_dkk <= 0 or incremental_savings <= 0.01:
        return normal("Higher target does not materially improve realized plan savings")

    return OpportunisticPlanDecision(
        plan=opportunistic_plan,
        settings=opportunistic_settings,
        active=True,
        reason=(
            f"Known-price cycle benefits from {opportunistic_settings.target_soc:g}%: "
            f"incremental savings {incremental_savings:.2f} DKK"
        ),
        incremental_savings_dkk=incremental_savings,
    )
