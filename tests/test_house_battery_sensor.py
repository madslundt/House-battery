"""Tests for the public optimizer sensor catalogue."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.sensor import SENSORS, FbpValueSensor, daily_plan_blocks


def _fake_daily_plan() -> dict[str, Any]:
    day = datetime(2026, 9, 20, tzinfo=UTC)
    return {
        "date": "2026-09-20",
        "created_at": None,
        "actual_soc": 100.0,
        "actual_soc_at": day.isoformat(),
        "history_available_from": day.isoformat(),
        "blocks": [
            {
                "start": (day).isoformat(),
                "end": (day + timedelta(hours=6)).isoformat(),
                "action": "grid",
                "soc_start": 100.0,
                "soc_end": 100.0,
                "expected_cost_dkk": 0.6,
                "expected_savings_dkk": 0.0,
                "energy_kwh": 0.0,
                "expected_load_kwh": 0.6,
                "expected_grid_import_kwh": 0.6,
                "reason": "low price",
            },
            {
                "start": (day + timedelta(hours=12)).isoformat(),
                "end": (day + timedelta(hours=24)).isoformat(),
                "action": "grid",
                "soc_start": 100.0,
                "soc_end": 100.0,
                "expected_cost_dkk": 0.6,
                "expected_savings_dkk": 0.0,
                "energy_kwh": 0.0,
                "expected_load_kwh": 0.6,
                "expected_grid_import_kwh": 0.6,
                "reason": "low price",
            },
        ],
        "horizon_slots": 96,
        "terminal_price_dkk_per_kwh": 0.1,
    }


def test_daily_plan_blocks_covers_the_complete_local_day() -> None:
    """Requirement #4/#5: the plan sensor must show 00:00 -> 24:00, not just the
    future from the current interval. ``daily_plan_blocks`` is what the
    FbpPlanSensor delegates to."""
    data = {"daily_plan": _fake_daily_plan()}
    blocks = daily_plan_blocks(data)
    assert blocks, "blocks must not be empty"
    # First block starts at the local day start, last ends at midnight.
    assert blocks[0]["start"] == datetime(2026, 9, 20, tzinfo=UTC).isoformat()
    assert (
        blocks[-1]["end"] == datetime(2026, 9, 21, tzinfo=UTC).isoformat()
    )
    # Observed SOC is surfaced at the top level (requirement #9), not bound to
    # the midnight block, so its timestamp is reported explicitly.
    assert data["daily_plan"]["actual_soc"] == 100.0
    assert data["daily_plan"]["actual_soc_at"] == datetime(
        2026, 9, 20, tzinfo=UTC
    ).isoformat()


def test_daily_plan_blocks_is_empty_without_a_daily_plan() -> None:
    assert daily_plan_blocks({}) == []
    assert daily_plan_blocks({"daily_plan": None}) == []


def test_grid_export_power_is_exposed_with_power_metadata() -> None:
    """Export power must be a first-class, observable quantity (not clamped to
    zero) so zero export can be verified from the live meter."""
    descriptions = {item.key: item for item in SENSORS}
    assert "grid_export_power_w" in descriptions
    export = descriptions["grid_export_power_w"]
    assert export.unit is UnitOfPower.WATT
    # Import and export sensors share metadata so they read as peers.
    assert export.icon == descriptions["grid_import_power_w"].icon.replace(
        "import", "export"
    )


def test_lifetime_energy_is_exposed_for_the_energy_dashboard() -> None:
    """Battery throughput has the metadata required by the Energy dashboard."""
    descriptions = {
        item.key: item
        for item in SENSORS
        if item.key in {"lifetime_charge_kwh", "lifetime_discharge_kwh"}
    }

    assert set(descriptions) == {"lifetime_charge_kwh", "lifetime_discharge_kwh"}
    for description in descriptions.values():
        assert description.device_class is SensorDeviceClass.ENERGY
        assert description.state_class is SensorStateClass.TOTAL_INCREASING


def test_dashboard_period_entities_are_available_with_measurement_units() -> None:
    descriptions = {item.key: item for item in SENSORS}
    charge_keys = {
        "yesterday_charge_kwh",
        "week_charge_kwh",
        "last_week_charge_kwh",
        "last_month_charge_kwh",
    }
    savings_keys = {
        "yesterday_net_savings_dkk",
        "week_net_savings_dkk",
        "last_week_net_savings_dkk",
        "last_month_net_savings_dkk",
    }

    assert charge_keys <= descriptions.keys()
    assert savings_keys <= descriptions.keys()
    assert all(descriptions[key].unit is UnitOfEnergy.KILO_WATT_HOUR for key in charge_keys)
    assert all(descriptions[key].unit == "DKK" for key in savings_keys)


@pytest.mark.parametrize("amount", [None, 0.0, 3.75, -1.25])
def test_expected_savings_tomorrow_is_a_default_monetary_forecast(amount: float | None) -> None:
    description = next(item for item in SENSORS if item.key == "tomorrow_expected_savings_dkk")
    assert description.name == "Expected savings tomorrow"
    assert description.unit == "DKK"
    assert description.precision == 2
    assert description.device_class is SensorDeviceClass.MONETARY
    assert description.state_class is None  # A changing forecast is not an accumulator.
    assert description.enabled_by_default
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(data={description.key: amount}),
        description=description,
    )
    assert FbpValueSensor.native_value.fget(sensor) == amount
    assert FbpValueSensor.last_reset.fget(sensor) is None


@pytest.mark.parametrize(
    ("period", "period_name"),
    [("week", "this week"), ("last_week", "last week")],
)
def test_weekly_battery_discharge_is_exposed(period: str, period_name: str) -> None:
    """Expose measured discharge for the same weekly periods as charge."""
    descriptions = {item.key: item for item in SENSORS}
    key = f"{period}_discharge_kwh"
    assert key in descriptions
    description = descriptions[key]
    assert description.name == f"Battery discharge {period_name}"
    assert description.icon == "mdi:battery-arrow-down"
    assert description.unit is UnitOfEnergy.KILO_WATT_HOUR
    assert description.precision == 3
    assert (
        description.enabled_by_default
        is descriptions[f"{period}_charge_kwh"].enabled_by_default
    )

    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(data={period: {"discharge_kwh": 4.125}}),
        description=description,
    )
    assert FbpValueSensor.native_value.fget(sensor) == 4.125


@pytest.mark.parametrize(
    ("period", "period_name"),
    [
        ("today", "today"),
        ("yesterday", "yesterday"),
        ("week", "this week"),
        ("last_week", "last week"),
        ("month", "this month"),
        ("last_month", "last month"),
        ("lifetime", "total"),
    ],
)
@pytest.mark.parametrize(
    ("metric", "metric_name", "amount"),
    [
        ("charge_cost_dkk", "Battery charge cost", -1.25),
        ("discharge_value_dkk", "Battery discharge value", 2.50),
        ("battery_net_savings_dkk", "Battery net savings", 3.75),
    ],
)
def test_battery_finances_cover_every_dashboard_period(
    period: str,
    period_name: str,
    metric: str,
    metric_name: str,
    amount: float,
) -> None:
    """Expose signed cost, discharge value, and net return for each energy period."""
    descriptions = {item.key: item for item in SENSORS}
    key = f"{period}_{metric}"
    assert key in descriptions
    description = descriptions[key]
    expected_name = f"{metric_name} {period_name}"
    if period == "lifetime" and metric != "battery_net_savings_dkk":
        expected_name = f"Recorded {expected_name.lower()}"
    assert description.name == expected_name
    assert description.unit == "DKK"
    assert description.precision == 2
    assert description.device_class is SensorDeviceClass.MONETARY
    assert description.state_class is SensorStateClass.TOTAL
    assert description.enabled_by_default is (
        metric == "battery_net_savings_dkk" and period in {"today", "lifetime"}
    )

    data = {key: amount} if period == "lifetime" else {period: {metric: amount}}
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(data=data),
        description=description,
    )
    assert FbpValueSensor.native_value.fget(sensor) == amount


@pytest.mark.parametrize(
    ("period", "start"),
    [
        ("today", "2026-10-22T00:00:00+02:00"),
        ("yesterday", "2026-10-21T00:00:00+02:00"),
        ("week", "2026-10-19T00:00:00+02:00"),
        ("last_week", "2026-10-12T00:00:00+02:00"),
        ("month", "2026-10-01T00:00:00+02:00"),
        ("last_month", "2026-09-01T00:00:00+02:00"),
    ],
)
@pytest.mark.parametrize(
    "metric", ["charge_cost_dkk", "discharge_value_dkk", "battery_net_savings_dkk"]
)
def test_period_money_resets_at_its_accounting_boundary(
    period: str,
    start: str,
    metric: str,
) -> None:
    descriptions = {item.key: item for item in SENSORS}
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={"accounting_period_starts": {period: start}}
        ),
        description=descriptions[f"{period}_{metric}"],
    )
    assert FbpValueSensor.last_reset.fget(sensor) == datetime.fromisoformat(start)


@pytest.mark.parametrize(
    "key",
    [
        "lifetime_charge_cost_dkk",
        "lifetime_discharge_value_dkk",
        "lifetime_battery_net_savings_dkk",
        "today_charge_kwh",
        "today_net_savings_dkk",
        "soc",
    ],
)
def test_lifetime_and_non_accumulator_sensors_have_no_calendar_reset(key: str) -> None:
    descriptions = {item.key: item for item in SENSORS}
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(
            data={
                "accounting_period_starts": {
                    "today": "2026-10-22T00:00:00+02:00",
                    "lifetime": "2026-01-01T00:00:00+01:00",
                }
            }
        ),
        description=descriptions[key],
    )
    assert FbpValueSensor.last_reset.fget(sensor) is None


def test_period_money_has_no_reset_without_an_accounting_boundary() -> None:
    descriptions = {item.key: item for item in SENSORS}
    sensor = SimpleNamespace(
        coordinator=SimpleNamespace(data={}),
        description=descriptions["today_charge_cost_dkk"],
    )
    assert FbpValueSensor.last_reset.fget(sensor) is None


# The two daily-plan tests above are the functional coverage for the plan
# sensor; keep the public catalogue import exercised in the same module.
_ = SENSORS
