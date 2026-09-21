"""Tests for the public optimizer sensor catalogue."""

import sys
from pathlib import Path

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

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
