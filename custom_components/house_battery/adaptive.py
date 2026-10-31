"""Opt-in net-savings policy gated by independent matched-SOC measurements."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from math import isclose, isfinite
from typing import Any, Literal

from .models import Plan, PlannerSettings

_METHOD = "matched_soc_grid_meter"
_REQUIRED_SAMPLES = 3
_POLICY_FIELDS = (
    "minimum_profit_dkk_per_kwh",
    "low_soc_charge_premium_dkk_per_kwh",
    "high_soc_discharge_discount_dkk_per_kwh",
)


def plan_economics(plan: Plan | None, settings: PlannerSettings) -> dict[str, float | None]:
    """Separate expected bill savings, wear and switching from notional credits.

    A plan's interval costs include wear and switching, but omit optimization
    preferences and forecast uncertainty. Stored terminal energy is an asset,
    not a bill saving. Negative import prices retain their actual sign.
    """
    keys = (
        "planned_bill_savings_dkk", "planned_wear_cost_dkk",
        "planned_switching_cost_dkk", "planned_net_savings_dkk",
    )
    if plan is None:
        return dict.fromkeys(keys)
    grid_cost = sum(slot.grid_import_wh * slot.price / 1000 for slot in plan.slots)
    bill_savings = sum(
        (slot.expected_load_wh - slot.grid_import_wh) * slot.price / 1000
        for slot in plan.slots
    )
    delivered_kwh = sum(
        max(0.0, slot.expected_load_wh - slot.grid_import_wh) for slot in plan.slots
    ) / 1000
    wear_cost = delivered_kwh * settings.degradation_cost_dkk_per_kwh
    switching_cost = max(0.0, plan.expected_cost_dkk - grid_cost - wear_cost)
    return dict(zip(keys, (
        bill_savings, wear_cost, switching_cost,
        bill_savings - wear_cost - switching_cost,
    ), strict=True))


@dataclass(frozen=True, slots=True)
class AdaptiveDecision:
    """Effective settings and explainable evidence, without changing manual values."""

    settings: PlannerSettings
    enabled: bool
    state: Literal["disabled", "learning", "active"]
    reason: str
    configured_minimum_profit_dkk_per_kwh: float
    automatic_changes: tuple[dict[str, Any], ...]
    recommendations: tuple[dict[str, Any], ...]
    qualification: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return independent serializable data without measurement source identifiers."""
        return deepcopy({
            "enabled": self.enabled,
            "state": self.state,
            "reason": self.reason,
            "configured_minimum_profit_dkk_per_kwh": (
                self.configured_minimum_profit_dkk_per_kwh
            ),
            "effective_minimum_profit_dkk_per_kwh": (
                self.settings.minimum_profit_dkk_per_kwh
            ),
            "automatic_changes": list(self.automatic_changes),
            "recommendations": list(self.recommendations),
            "qualification": self.qualification,
        })


def _qualification(
    settings: PlannerSettings, summary: dict[str, Any],
) -> dict[str, Any]:
    source = summary.get("source")
    bound = isinstance(source, str) and bool(source.strip())
    samples = summary.get("sample_count")
    samples = samples if isinstance(samples, int) and not isinstance(samples, bool) else 0
    measured = summary.get("planning_efficiency_pct")
    valid_efficiency = (
        isinstance(measured, (int, float)) and not isinstance(measured, bool)
        and isfinite(measured) and 50 <= measured <= 98
    )
    applied = valid_efficiency and isclose(
        settings.round_trip_efficiency, measured / 100, rel_tol=0, abs_tol=1e-6,
    )
    evidence: dict[str, Any] = {
        "meter_bound": bound,
        "method": summary.get("method") if isinstance(summary.get("method"), str) else None,
        "measurement_status": (
            summary.get("status") if isinstance(summary.get("status"), str) else None
        ),
        "accepted_samples": max(0, samples),
        "required_samples": _REQUIRED_SAMPLES,
        "planning_efficiency_pct": measured if valid_efficiency else None,
        "efficiency_applied": bool(applied),
        "measurement_reason": (
            summary.get("reason") if isinstance(summary.get("reason"), str) else ""
        ),
    }
    if not bound:
        code, message = "meter_unbound", "An independent real-grid power sensor is required"
    elif evidence["method"] != _METHOD:
        code, message = "invalid_method", "Efficiency must come from matched-SOC real-grid measurements"
    elif evidence["measurement_status"] != "ready":
        code, message = "measurement_pending", "Awaiting fresh, stable matched-SOC efficiency evidence"
    elif samples < _REQUIRED_SAMPLES:
        code, message = "insufficient_samples", "At least three independent matched-SOC cycles are required"
    elif not valid_efficiency:
        code, message = "invalid_efficiency", "Qualified planning efficiency must be between 50% and 98%"
    elif not applied:
        code, message = "efficiency_not_applied", "Qualified efficiency has not been applied to planning settings"
    else:
        code, message = "qualified", "Fresh matched-SOC efficiency is qualified and applied to planning"
    return {**evidence, "code": code, "message": message}


def build_recommendations(
    *, enabled: bool, state: str, qualification: dict[str, Any],
    configured_settings: PlannerSettings, effective_settings: PlannerSettings,
) -> list[dict[str, Any]]:
    """Explain only measured prerequisites and the policy this module controls.

    Missing bindings need manual configuration. A bound learner automatically
    waits for matching SOC, stable charging baselines, and fresh accepted cycles.
    Do not infer a capacity purchase, tariff fault, or forecast failure without
    evidence for those decisions.
    """
    code = qualification["code"]
    if not enabled:
        measurement_status, severity = "disabled", "info"
        measurement_message = "Automatic adaptation is off; manual economic settings remain in use"
    elif code in {"meter_unbound", "invalid_method"}:
        measurement_status, severity = "manual_action", "warning"
        measurement_message = (
            "Bind an independent real-grid power sensor for matched-SOC efficiency learning"
        )
    elif code == "qualified":
        measurement_status, severity = "satisfied", "info"
        measurement_message = "Qualified measured efficiency is applied to planning"
    else:
        measurement_status, severity = "awaiting_measurement", "info"
        measurement_message = (
            "Wait for three fresh, consistent matched-SOC cycles with stable grid baselines; "
            "manual economic settings remain in use until qualification succeeds"
        )
    recommendations = [{
        "id": "efficiency_measurement", "severity": severity,
        "status": measurement_status, "message": measurement_message,
        "evidence": dict(qualification),
    }]
    recommendations.append({
        "id": "net_savings_policy", "severity": "info",
        "status": "automatic" if state == "active" else "disabled" if not enabled else "awaiting_measurement",
        "message": (
            "Automatically optimize expected net savings after configured wear, conversion losses, "
            "and forecast uncertainty, without a separate profit hurdle or SOC price preferences"
            if state == "active" else
            "Net-savings adaptation waits for qualified efficiency; configured wear and physical limits are preserved"
            if enabled else
            "Manual profit hurdle and SOC price preferences are preserved"
        ),
        "evidence": {
            "configured_minimum_profit_dkk_per_kwh": configured_settings.minimum_profit_dkk_per_kwh,
            "effective_minimum_profit_dkk_per_kwh": effective_settings.minimum_profit_dkk_per_kwh,
            "degradation_cost_dkk_per_kwh": effective_settings.degradation_cost_dkk_per_kwh,
            "round_trip_efficiency_pct": effective_settings.round_trip_efficiency * 100,
        },
    })
    return recommendations


def adaptive_policy(
    settings: PlannerSettings, *, enabled: bool, efficiency_summary: dict[str, Any],
) -> AdaptiveDecision:
    """Apply opt-in economics only after the independent learner qualifies.

    The caller must first apply the learner's qualified efficiency to settings.
    This function never guesses efficiency, changes the selected meter, mutates
    manual settings, or changes capacity, power limits, reserve, target, or wear.
    The learner owns expiry and source-reset validation before creating its
    summary; a stale or reset summary immediately restores the manual policy.
    """
    qualification = _qualification(settings, efficiency_summary)
    state: Literal["disabled", "learning", "active"] = (
        "disabled" if not enabled else
        "active" if qualification["code"] == "qualified" else "learning"
    )
    effective = settings
    changes: tuple[dict[str, Any], ...] = ()
    if state == "active":
        changes = tuple(
            {"field": field, "configured": getattr(settings, field), "effective": 0.0}
            for field in _POLICY_FIELDS if getattr(settings, field) != 0
        )
        effective = replace(settings, **dict.fromkeys(_POLICY_FIELDS, 0.0))
    reason = (
        "Automatic adaptation is disabled; manual economic settings are preserved"
        if state == "disabled" else
        "Qualified measured efficiency enables net-savings economics after battery wear"
        if state == "active" else qualification["message"]
    )
    recommendations = build_recommendations(
        enabled=enabled, state=state, qualification=qualification,
        configured_settings=settings, effective_settings=effective,
    )
    return AdaptiveDecision(
        settings=effective, enabled=enabled, state=state, reason=reason,
        configured_minimum_profit_dkk_per_kwh=settings.minimum_profit_dkk_per_kwh,
        automatic_changes=changes, recommendations=tuple(recommendations),
        qualification=qualification,
    )
