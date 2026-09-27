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
    """Use the higher-SOC plan for a profitable cycle or valuable carryover.

    Compare the optimizer's full risk-adjusted objective, including forecast
    uncertainty, degradation, switching, and terminal carryover value. This
    keeps the optional higher ceiling from being accepted on nominal savings
    when it makes the actual optimization objective worse.
    """
    incremental_realized_savings = (
        opportunistic_plan.realized_savings_dkk - normal_plan.realized_savings_dkk
    )
    incremental_expected_savings = (
        opportunistic_plan.expected_savings_dkk - normal_plan.expected_savings_dkk
    )

    def normal(
        reason: str, savings: float = incremental_expected_savings
    ) -> OpportunisticPlanDecision:
        return OpportunisticPlanDecision(
            plan=normal_plan,
            settings=normal_settings,
            active=False,
            reason=reason,
            incremental_savings_dkk=max(0.0, savings),
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
    extra_charge_above_target_wh = sum(
        slot.battery_charge_wh
        for slot in opportunistic_plan.slots
        if slot.soc_end > normal_settings.target_soc + soc_tolerance
    )
    peak_slot = max(opportunistic_plan.slots, key=lambda slot: slot.soc_end)
    if extra_charge_above_target_wh <= energy_tolerance_wh:
        return normal("Higher target does not add meaningful charging")
    if peak_slot.soc_end <= normal_settings.target_soc + soc_tolerance:
        return normal("Plan does not need storage above the normal target")

    normal_objective = (
        normal_plan.optimization_objective_dkk
        if normal_plan.optimization_objective_dkk is not None
        else normal_plan.expected_cost_dkk - normal_plan.terminal_value_dkk
    )
    opportunistic_objective = (
        opportunistic_plan.optimization_objective_dkk
        if opportunistic_plan.optimization_objective_dkk is not None
        else opportunistic_plan.expected_cost_dkk
        - opportunistic_plan.terminal_value_dkk
    )
    objective_improvement = normal_objective - opportunistic_objective
    if objective_improvement <= 0.01:
        return normal(
            "Higher target does not improve the risk-adjusted plan objective",
            incremental_realized_savings,
        )

    return OpportunisticPlanDecision(
        plan=opportunistic_plan,
        settings=opportunistic_settings,
        active=True,
        reason=(
            f"Risk-adjusted plan objective improves by {objective_improvement:.2f} DKK "
            f"at {opportunistic_settings.target_soc:g}% SOC"
        ),
        incremental_savings_dkk=max(0.0, incremental_expected_savings),
    )
