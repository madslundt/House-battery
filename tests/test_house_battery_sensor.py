"""Tests for the public optimizer sensor catalogue."""

import sys
from pathlib import Path

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.sensor import SENSORS


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
