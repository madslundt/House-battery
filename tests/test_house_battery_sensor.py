"""Tests for the public optimizer sensor catalogue."""

import sys
from pathlib import Path

from homeassistant.components.sensor import SensorStateClass

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.sensor import SENSORS


def test_lifetime_charge_is_exposed_as_cumulative_energy() -> None:
    """Charge throughput is available for dashboards and long-term analysis."""
    description = next(item for item in SENSORS if item.key == "lifetime_charge_kwh")

    assert description.name == "Battery charge total"
    assert description.state_class is SensorStateClass.TOTAL_INCREASING
