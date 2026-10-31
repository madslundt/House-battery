"""Adaptive economics waits for independent, qualified efficiency evidence."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.adaptive import adaptive_policy, plan_economics
from house_battery.efficiency import MatchedSocEfficiency
from house_battery.models import Plan, PlannedSlot, PlannerSettings


def settings() -> PlannerSettings:
    return PlannerSettings(
        capacity_wh=1958, reserve_soc=10, target_soc=90,
        charge_power_w=1100, discharge_power_w=800,
        round_trip_efficiency=0.80, degradation_cost_dkk_per_kwh=0.35,
        minimum_profit_dkk_per_kwh=0.50, switching_penalty_dkk=0.05,
        low_soc_charge_premium_dkk_per_kwh=0.25,
        high_soc_discharge_discount_dkk_per_kwh=0.25,
        partial_charge_targets=(80, 90),
    )


def qualified(**changes: object) -> dict:
    result = {
        "source": "independent-total-grid-source-fingerprint",
        "method": "matched_soc_grid_meter", "status": "ready",
        "planning_efficiency_pct": 80.0, "sample_count": 3,
        "reason": "Three matching settled SOC cycles measured",
    }
    result.update(changes)
    return result


def test_disabled_policy_preserves_manual_settings_even_with_qualified_evidence() -> None:
    manual = settings()
    result = adaptive_policy(manual, enabled=False, efficiency_summary=qualified())
    assert result.settings is manual
    assert result.as_dict()["state"] == "disabled"
    assert result.as_dict()["automatic_changes"] == []


@pytest.mark.parametrize("summary", [
    {}, qualified(source=""), qualified(source=None),
    qualified(method="battery_reported_power"), qualified(method=None),
    qualified(status="learning"), qualified(status="stale"),
    qualified(status="not_configured"), qualified(status="source_changed"),
    qualified(sample_count=2), qualified(sample_count=True),
    qualified(sample_count="3"), qualified(sample_count=float("inf")),
    qualified(planning_efficiency_pct=None),
    qualified(planning_efficiency_pct=float("nan")),
    qualified(planning_efficiency_pct=float("inf")),
    qualified(planning_efficiency_pct=True),
    qualified(planning_efficiency_pct=49.99),
    qualified(planning_efficiency_pct=98.01),
    qualified(planning_efficiency_pct=75),
])
def test_unqualified_or_unapplied_measurement_cannot_change_economics(summary: dict) -> None:
    manual = settings()
    result = adaptive_policy(manual, enabled=True, efficiency_summary=summary)
    assert result.settings is manual
    assert result.as_dict()["state"] == "learning"
    assert result.as_dict()["effective_minimum_profit_dkk_per_kwh"] == 0.5
    assert result.as_dict()["automatic_changes"] == []
    assert result.as_dict()["recommendations"]


@pytest.mark.parametrize("efficiency", [0.50, 0.63, 0.80, 0.98])
def test_qualified_policy_removes_virtual_preferences_and_preserves_every_other_setting(
    efficiency: float,
) -> None:
    manual = replace(settings(), round_trip_efficiency=efficiency)
    result = adaptive_policy(
        manual, enabled=True,
        efficiency_summary=qualified(planning_efficiency_pct=efficiency * 100),
    )
    assert result.settings == replace(
        manual, minimum_profit_dkk_per_kwh=0,
        low_soc_charge_premium_dkk_per_kwh=0,
        high_soc_discharge_discount_dkk_per_kwh=0,
    )
    assert manual.minimum_profit_dkk_per_kwh == 0.5
    assert manual.round_trip_efficiency == efficiency
    data = result.as_dict()
    assert data["state"] == "active"
    assert data["configured_minimum_profit_dkk_per_kwh"] == 0.5
    assert data["effective_minimum_profit_dkk_per_kwh"] == 0
    assert {change["field"] for change in data["automatic_changes"]} == {
        "minimum_profit_dkk_per_kwh", "low_soc_charge_premium_dkk_per_kwh",
        "high_soc_discharge_discount_dkk_per_kwh",
    }


def test_enabling_an_already_net_savings_policy_has_no_fictitious_changes() -> None:
    manual = replace(
        settings(), minimum_profit_dkk_per_kwh=0,
        low_soc_charge_premium_dkk_per_kwh=0,
        high_soc_discharge_discount_dkk_per_kwh=0,
    )
    result = adaptive_policy(manual, enabled=True, efficiency_summary=qualified())
    assert result.as_dict()["state"] == "active"
    assert result.as_dict()["automatic_changes"] == []


def test_recommendations_distinguish_missing_binding_from_automatic_learning() -> None:
    missing = adaptive_policy(settings(), enabled=True, efficiency_summary={}).as_dict()
    learning = adaptive_policy(
        settings(), enabled=True,
        efficiency_summary=qualified(
            status="learning", planning_efficiency_pct=None, sample_count=1,
            reason="Other grid loads make the charge baseline uncertain",
        ),
    ).as_dict()
    assert missing["recommendations"][0]["status"] == "manual_action"
    assert learning["recommendations"][0]["status"] == "awaiting_measurement"
    evidence = learning["recommendations"][0]["evidence"]
    assert evidence["accepted_samples"] == 1
    assert evidence["required_samples"] == 3
    assert "baseline" in evidence["measurement_reason"]
    for result in (missing, learning):
        assert all(set(item) == {"id", "severity", "status", "message", "evidence"}
                   for item in result["recommendations"])


def test_stale_or_changed_source_reverts_to_manual_policy_without_reusing_evidence() -> None:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    model = MatchedSocEfficiency()
    model.configure("selected-independent-grid-source")
    model.accepted_windows = [
        {"start": (now - timedelta(days=index + 1, hours=2)).isoformat(),
         "end": (now - timedelta(days=index + 1)).isoformat(),
         "charge_kwh": 1, "discharge_kwh": 0.8, "efficiency": 0.8}
        for index in reversed(range(3))
    ]
    assert adaptive_policy(settings(), enabled=True,
                           efficiency_summary=model.summary(now)).as_dict()["state"] == "active"
    stale = model.summary(now + timedelta(days=31))
    assert adaptive_policy(settings(), enabled=True,
                           efficiency_summary=stale).settings == settings()
    model.configure("different-independent-grid-source")
    changed = model.summary(now)
    assert changed["sample_count"] == 0
    assert adaptive_policy(settings(), enabled=True,
                           efficiency_summary=changed).as_dict()["state"] == "learning"


def test_exported_decision_does_not_expose_source_or_mutate_the_summary() -> None:
    summary = qualified()
    original = dict(summary)
    result = adaptive_policy(settings(), enabled=True, efficiency_summary=summary)
    exported = result.as_dict()
    assert summary == original
    assert summary["source"] not in str(exported)
    exported["automatic_changes"].clear()
    exported["recommendations"][0]["evidence"]["required_samples"] = 99
    assert len(result.as_dict()["automatic_changes"]) == 3
    assert result.as_dict()["recommendations"][0]["evidence"]["required_samples"] == 3


def economics_plan(*, charge_price: float, terminal: float = 0) -> Plan:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    from house_battery.models import Action

    charge_cost = 1.2 * charge_price + 0.05
    discharge_cost = 0.2 * 3 + 0.8 * 0.35 + 0.05
    rows = (
        PlannedSlot(
            now, now + timedelta(hours=1), Action.CHARGE, charge_price,
            200, 1200, 900, 0, 10, 60, charge_cost, 0.2 * charge_price,
            "low-SOC premium changes optimization only",
            price_source="forecast", price_uncertainty_dkk_per_kwh=0.4,
        ),
        PlannedSlot(
            now + timedelta(hours=1), now + timedelta(hours=2), Action.BATTERY, 3,
            1000, 200, 0, 900, 60, 10, discharge_cost, 3,
            "discharge preference changes optimization only",
            price_source="forecast", price_uncertainty_dkk_per_kwh=0.4,
        ),
    )
    expected = charge_cost + discharge_cost
    baseline = 0.2 * charge_price + 3
    return Plan(
        now, rows, expected, baseline, baseline - expected + terminal, 1.8, 2,
        "economic test", terminal, optimization_objective_dkk=99,
    )


def test_plan_economics_separates_bill_savings_from_wear_switches_and_terminal() -> None:
    plan = economics_plan(charge_price=1, terminal=7)
    result = plan_economics(plan, settings())
    assert result == pytest.approx({
        "planned_bill_savings_dkk": 1.4,
        "planned_wear_cost_dkk": 0.28,
        "planned_switching_cost_dkk": 0.1,
        "planned_net_savings_dkk": 1.02,
    })
    assert result["planned_net_savings_dkk"] == pytest.approx(plan.realized_savings_dkk)
    assert result["planned_net_savings_dkk"] != plan.expected_savings_dkk


def test_plan_economics_retains_negative_charge_prices_and_ignores_virtual_costs() -> None:
    plan = economics_plan(charge_price=-1, terminal=7)
    result = plan_economics(plan, settings())
    assert result["planned_bill_savings_dkk"] == pytest.approx(3.4)
    assert result["planned_wear_cost_dkk"] == pytest.approx(0.28)
    assert result["planned_switching_cost_dkk"] == pytest.approx(0.1)
    assert result["planned_net_savings_dkk"] == pytest.approx(3.02)
    assert result["planned_net_savings_dkk"] == pytest.approx(plan.realized_savings_dkk)


def test_no_plan_economics_is_unavailable_instead_of_zero() -> None:
    assert plan_economics(None, settings()) == {
        "planned_bill_savings_dkk": None, "planned_wear_cost_dkk": None,
        "planned_switching_cost_dkk": None, "planned_net_savings_dkk": None,
    }


def test_rounding_noise_does_not_create_negative_switching_costs() -> None:
    plan = economics_plan(charge_price=1)
    plan = replace(plan, expected_cost_dkk=1.2 + 0.6 + 0.28 - 1e-10)
    assert plan_economics(plan, settings())["planned_switching_cost_dkk"] == 0
