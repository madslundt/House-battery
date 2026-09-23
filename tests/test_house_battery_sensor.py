"""Tests for the public optimizer sensor catalogue."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.sensor import SENSORS, daily_plan_blocks


def _fake_daily_plan() -> dict[str, Any]:
    day = datetime(2026, 9, 20, tzinfo=timezone.utc)
    return {
        "date": "2026-09-20",
        "created_at": None,
        "actual_soc": 100.0,
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
                "actual_soc": 100.0,
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
                "actual_soc": 100.0,
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
    assert blocks[0]["start"] == datetime(2026, 9, 20, tzinfo=timezone.utc).isoformat()
    assert (
        blocks[-1]["end"] == datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat()
    )
    # Observed SOC is surfaced on the first block (requirement #9).
    assert blocks[0]["actual_soc"] == 100.0


def test_daily_plan_blocks_is_empty_without_a_daily_plan() -> None:
    assert daily_plan_blocks({}) == []
    assert daily_plan_blocks({"daily_plan": None}) == []


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


# The two daily-plan tests above are the functional coverage for the plan
# sensor; keep the public catalogue import exercised in the same module.
_ = SENSORS
