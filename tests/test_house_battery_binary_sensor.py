"""Binary sensor tests: health, grid availability and export detection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.binary_sensor import (
    FbpExportDetectedBinarySensor,
    FbpExportSafetyFaultBinarySensor,
    FbpGridAvailableBinarySensor,
    FbpHealthBinarySensor,
)


def _coordinator(
    data: dict[str, object],
    config: dict[str, object] | None = None,
    *,
    entry_id: str = "abc123",
) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        config=config or {},
        entry=SimpleNamespace(entry_id=entry_id),
        device_info=None,
    )


def test_health_sensor_flags_the_coordinator_healthy_flag() -> None:
    sensor = FbpHealthBinarySensor(
        _coordinator({"healthy": False, "health_problems": ["grid down"]})
    )
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["reason"] is None


def test_health_sensor_clears_when_healthy() -> None:
    sensor = FbpHealthBinarySensor(_coordinator({"healthy": True}))
    assert sensor.is_on is False


def test_grid_available_sensor_reports_the_bound_signal() -> None:
    sensor = FbpGridAvailableBinarySensor(
        _coordinator(
            {"grid_available": True},
            {"grid_available_entity": "binary_sensor.grid_available"},
        )
    )
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["source"] == "binary_sensor.grid_available"


def test_export_detected_sensor_tracks_the_meter() -> None:
    sensor = FbpExportDetectedBinarySensor(
        _coordinator({"export_detected": True, "export_power_w": 42.0})
    )
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["power_w"] == 42.0


def test_export_detected_sensor_off_when_no_export() -> None:
    sensor = FbpExportDetectedBinarySensor(_coordinator({"export_detected": False}))
    assert sensor.is_on is False


def test_export_safety_fault_sensor_latched() -> None:
    sensor = FbpExportSafetyFaultBinarySensor(
        _coordinator({"export_safety_fault": True})
    )
    assert sensor.is_on is True
    assert "reset" in sensor.extra_state_attributes
