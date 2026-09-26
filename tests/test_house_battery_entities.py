"""Regression tests for entity availability contracts."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import (
    CONF_PRICE_FORECAST_ENTITIES,
    OVERRIDE_AUTO,
    OVERRIDE_OPTIONS,
    PLATFORMS,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.number import FbpNativeSocNumber
from house_battery.sensor import (
    FbpCurrentPlanSlotSensor,
    FbpLocalLoadDiagnosticsSensor,
    FbpModeSensor,
    FbpPlanExecutionSensor,
    FbpPlannedLoadPowerSensor,
    daily_plan_blocks,
)
from house_battery.switch import (
    FbpExternalForecastSwitch,
    FbpOpportunisticChargingSwitch,
)


def test_native_soc_numbers_read_the_coordinator_control_keys() -> None:
    """Direct-control entities stay available when TCP controls were read."""
    coordinator = SimpleNamespace(
        data={"native_min_soc": 10.0, "native_max_soc": 90.0}
    )
    minimum = SimpleNamespace(coordinator=coordinator, key="minimum")
    maximum = SimpleNamespace(coordinator=coordinator, key="maximum")

    assert FbpNativeSocNumber.available.fget(minimum)
    assert FbpNativeSocNumber.native_value.fget(minimum) == 10.0
    assert FbpNativeSocNumber.available.fget(maximum)
    assert FbpNativeSocNumber.native_value.fget(maximum) == 90.0


def test_external_forecast_switch_accepts_multi_source_configuration() -> None:
    """The switch is available for current multi-source forecast entries."""
    coordinator = SimpleNamespace(
        config={CONF_PRICE_FORECAST_ENTITIES: ["sensor.tariff_forecast"]}
    )
    coordinator._forecast_entities = lambda: Fbp1200Coordinator._forecast_entities(
        coordinator
    )
    switch = SimpleNamespace(coordinator=coordinator)

    assert Fbp1200Coordinator._forecast_entities(coordinator) == [
        "sensor.tariff_forecast"
    ]
    assert FbpExternalForecastSwitch.available.fget(switch)


def test_opportunistic_full_charge_switch_reports_policy_state() -> None:
    coordinator = SimpleNamespace(
        runtime=SimpleNamespace(
            opportunistic_charging_enabled=True,
            settings={"target_soc": 90.0, "opportunistic_target_soc": 100.0},
        ),
        data={
            "extra_storage_active": True,
            "effective_target_soc": 100.0,
            "opportunistic_incremental_savings_dkk": 0.79,
            "extra_storage_reason": "Known-price cycle is profitable",
        },
    )
    switch = SimpleNamespace(coordinator=coordinator)

    assert FbpOpportunisticChargingSwitch.is_on.fget(switch)
    attributes = FbpOpportunisticChargingSwitch.extra_state_attributes.fget(switch)
    assert attributes["active"] is True
    assert attributes["normal_target_soc"] == 90.0
    assert attributes["opportunistic_target_soc"] == 100.0
    assert attributes["effective_target_soc"] == 100.0


def test_local_load_diagnostics_exposes_scopes_without_selecting_one() -> None:
    coordinator = SimpleNamespace(
        data={
            "local_load_diagnostics": {
                "meter_total_active_power_w": 442,
                "smart_load_power_w": 106,
                "backup_load_power_w": 0,
                "off_grid_load_power_per_unit_w": [0],
                "off_grid_load_power_total_w": 0,
            }
        },
        config={},
    )
    sensor = SimpleNamespace(coordinator=coordinator)

    assert FbpLocalLoadDiagnosticsSensor.native_value.fget(sensor) == "ready"
    attributes = FbpLocalLoadDiagnosticsSensor.extra_state_attributes.fget(sensor)
    assert attributes["smart_load_power_w"] == 106
    assert "fail closed" in attributes["selection_status"]


def test_local_load_diagnostics_are_unavailable_without_any_candidate_value() -> None:
    coordinator = SimpleNamespace(
        data={
            "local_load_diagnostics": {
                "meter_total_active_power_w": 442,
                "smart_load_power_w": None,
                "backup_load_power_w": None,
                "off_grid_load_power_per_unit_w": [None, None],
                "off_grid_load_power_total_w": None,
            }
        },
        config={},
    )
    sensor = SimpleNamespace(coordinator=coordinator)

    assert FbpLocalLoadDiagnosticsSensor.native_value.fget(sensor) == "unavailable"


def test_local_load_diagnostics_report_inconsistent_totals() -> None:
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={
                "local_load_diagnostics": {
                    "off_grid_load_power_total_w": 127,
                    "off_grid_load_validation_error": (
                        "per-storage off-grid total (127 W) disagrees with device "
                        "total backup power (12.7 W)"
                    ),
                }
            }
        )
    )

    assert FbpLocalLoadDiagnosticsSensor.native_value.fget(sensor) == "inconsistent"


def test_battery_activity_uses_physical_power_not_configured_mode() -> None:
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={
                "observed_action": "battery",
                "battery_charge_power_w": 0,
                "battery_output_power_w": 0,
            }
        )
    )

    assert FbpModeSensor.native_value.fget(sensor) == "idle"


def test_current_plan_slot_exposes_auditable_planner_inputs() -> None:
    data = {
        "plan": {
            "slots": [
                {
                    "start": "2026-09-22T10:00:00+00:00",
                    "end": "2026-09-22T10:15:00+00:00",
                    "action": "charge",
                    "price": 0.25,
                    "price_source": "forecast",
                    "price_uncertainty_dkk_per_kwh": 0.1,
                    "expected_load_wh": 125,
                    "grid_import_wh": 425,
                    "battery_charge_wh": 300,
                    "battery_discharge_wh": 0,
                    "soc_start": 20,
                    "soc_end": 35,
                    "interval_cost_dkk": 0.10625,
                    "baseline_cost_dkk": 0.03125,
                    "reason": "Charge during a cheap interval",
                }
            ]
        },
        "battery_charge_power_w": 900,
        "battery_output_power_w": 0,
        "command_result": "mode set",
    }
    coordinator = SimpleNamespace(data=data)
    slot_sensor = SimpleNamespace(coordinator=coordinator)
    execution_sensor = SimpleNamespace(coordinator=coordinator)
    load_sensor = SimpleNamespace(coordinator=coordinator)

    assert (
        FbpCurrentPlanSlotSensor.native_value.fget(slot_sensor)
        == "2026-09-22T10:00:00+00:00"
    )
    attributes = FbpCurrentPlanSlotSensor.extra_state_attributes.fget(slot_sensor)
    assert attributes["price_source"] == "forecast"
    assert attributes["expected_average_load_w"] == 500
    assert attributes["planned_battery_charge_kwh"] == 0.3
    assert FbpPlannedLoadPowerSensor.native_value.fget(load_sensor) == 500
    assert FbpPlanExecutionSensor.native_value.fget(execution_sensor) == "matching"


def test_operation_plan_blocks_expose_expected_grid_use_and_cost() -> None:
    # FbpPlanSensor now reads the persisted daily plan's merged blocks
    # (already produced by DailyPlan.view_dict), not raw price slots.
    blocks = daily_plan_blocks(
        {
            "daily_plan": {
                "date": "2026-09-22",
                "blocks": [
                    {
                        "start": "2026-09-22T10:00:00+00:00",
                        "end": "2026-09-22T10:15:00+00:00",
                        "action": "grid",
                        "expected_load_kwh": 0.125,
                        "expected_grid_import_kwh": 0.125,
                        "expected_cost_dkk": 0.25,
                        "expected_savings_dkk": 0.0,
                        "energy_kwh": 0.0,
                        "soc_start": 52.0,
                        "soc_end": 52.0,
                        "actual_soc": 100.0,
                        "reason": "Grid supplies the load",
                    }
                ],
            }
        }
    )

    assert blocks[0]["expected_load_kwh"] == 0.125
    assert blocks[0]["expected_grid_import_kwh"] == 0.125
    assert blocks[0]["expected_cost_dkk"] == 0.25


def test_plan_execution_reports_an_unexpected_physical_movement() -> None:
    coordinator = SimpleNamespace(
        data={
            "plan": {
                "slots": [
                    {
                        "start": "2026-09-22T10:00:00+00:00",
                        "end": "2026-09-22T10:15:00+00:00",
                        "battery_charge_wh": 0,
                        "battery_discharge_wh": 0,
                    }
                ]
            },
            "battery_charge_power_w": 0,
            "battery_output_power_w": 200,
        }
    )
    sensor = SimpleNamespace(coordinator=coordinator)

    assert FbpPlanExecutionSensor.native_value.fget(sensor) == "unexpected_battery_activity"


def test_operating_mode_is_not_a_user_writable_entity() -> None:
    """Only the optimizer may command the direct battery operating mode."""
    # A select platform exists, but only for the storage override (auto / charge /
    # battery / grid). It never exposes the inverter's native operating mode, which
    # the optimizer alone may command.
    assert "select" in PLATFORMS
    assert set(OVERRIDE_OPTIONS) == {OVERRIDE_AUTO, "charge", "battery", "grid"}
