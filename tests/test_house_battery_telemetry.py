"""Tests for the single-point grid-flow normalisation used everywhere."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import BatteryTelemetry, normalize_grid_flow


def test_positive_grid_power_is_import_only() -> None:
    import_w, export_w = normalize_grid_flow(500.0)
    assert (import_w, export_w) == (500.0, 0.0)


def test_negative_grid_power_is_export_only() -> None:
    import_w, export_w = normalize_grid_flow(-320.0)
    assert (import_w, export_w) == (0.0, 320.0)


def test_zero_grid_power_has_no_import_or_export() -> None:
    assert normalize_grid_flow(0.0) == (0.0, 0.0)


def test_opposite_sign_meter_is_normalised_without_touching_every_caller() -> None:
    # Inverted meter: +300 is export.
    import_w, export_w = normalize_grid_flow(300.0, sign=-1.0)
    assert (import_w, export_w) == (0.0, 300.0)


def test_battery_telemetry_exposes_both_raw_and_decomposed_grid_values() -> None:
    telemetry = BatteryTelemetry(
        soc=50.0,
        load_w=200.0,
        grid_power_w=-90.0,
        grid_import_w=0.0,
        grid_export_w=90.0,
        charge_w=0.0,
        discharge_w=0.0,
        timestamp=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )
    assert telemetry.grid_power_w == -90.0
    assert telemetry.grid_export_w == 90.0
    assert telemetry.grid_import_w == 0.0
