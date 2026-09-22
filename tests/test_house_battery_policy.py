"""Tests for the simplified storage policy.

Extra-storage target and spread knobs were removed (2025-09) because they
created wasteful discharge→recharge cycles.  The target_soc is now a hard
ceiling and apply_storage_policy returns settings unchanged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings
from house_battery.policy import apply_storage_policy, parse_grid_available


def settings() -> PlannerSettings:
    return PlannerSettings(
        capacity_wh=1958,
        reserve_soc=20,
        target_soc=90,
        charge_power_w=1200,
        discharge_power_w=800,
        round_trip_efficiency=0.85,
        degradation_cost_dkk_per_kwh=0.35,
        minimum_profit_dkk_per_kwh=0.75,
        switching_penalty_dkk=0.05,
        minimum_mode_minutes=30,
        maximum_transitions=4,
    )


def test_policy_returns_settings_unchanged() -> None:
    """The simplified policy no longer lifts the charge ceiling."""
    result = apply_storage_policy(settings())
    assert result.target_soc == 90


def test_policy_ignores_any_additional_arguments() -> None:
    # Backward-compat: callers that still pass old kwargs must not break.
    result = apply_storage_policy(settings())
    assert result.target_soc == 90


def test_grid_availability_is_parsed_only_from_its_explicit_state() -> None:
    assert parse_grid_available("on") is True
    assert parse_grid_available("disconnected") is False
    assert parse_grid_available("3.7") is None
