"""Regression tests for entity availability contracts."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import CONF_PRICE_FORECAST_ENTITIES
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.number import FbpNativeSocNumber
from house_battery.switch import FbpExternalForecastSwitch


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
