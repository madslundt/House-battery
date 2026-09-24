"""Tests for auditable interval savings and battery-use accounting."""

import sys
from datetime import UTC, datetime
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.accounting import (
    EnergyLedger,
    IntervalAccumulator,
    LedgerInterval,
    calendar_period_bounds,
)
from house_battery.models import Action


def test_complete_battery_interval_books_net_savings_after_wear() -> None:
    accumulator = IntervalAccumulator(datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    for _ in range(15):
        accumulator.add(
            seconds=60,
            load_w=500,
            grid_power_w=0,
            grid_sign=1.0,
            charge_w=0,
            discharge_w=500,
            price=2.0,
            soc=80,
            action=Action.BATTERY,
        )
    result = EnergyLedger().close(accumulator, degradation_cost=0.35)
    assert result.quality == "good"
    assert result.baseline_cost_dkk == pytest.approx(0.25)
    assert result.degradation_dkk == pytest.approx(0.04375)
    assert result.net_savings_dkk == pytest.approx(0.20625)
    assert result.end == "2026-09-20T10:15:00+00:00"


def test_incomplete_interval_never_claims_savings() -> None:
    accumulator = IntervalAccumulator(datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    accumulator.add(
        seconds=60,
        load_w=500,
        grid_power_w=0,
        grid_sign=1.0,
        charge_w=0,
        discharge_w=500,
        price=2.0,
        soc=80,
        action=Action.BATTERY,
    )
    result = EnergyLedger().close(accumulator, degradation_cost=0.35)
    assert result.quality == "incomplete"
    assert result.net_savings_dkk is None


def test_grid_export_is_recorded_distinctly_and_not_clamped_away() -> None:
    # Negative grid power is export. The old behaviour clamped it to zero and
    # hid it; export must now be captured as its own quantity in a *good*
    # interval, and must never be credited as avoided import cost.
    accumulator = IntervalAccumulator(datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    for _ in range(12):
        accumulator.add(
            seconds=60,
            load_w=100,
            grid_power_w=-250,
            grid_sign=1.0,
            charge_w=0,
            discharge_w=0,
            price=2.0,
            soc=60,
            action=Action.GRID,
        )
    result = EnergyLedger().close(accumulator, degradation_cost=0.35)
    assert result.quality == "good"
    assert result.grid_import_kwh == pytest.approx(0.0)
    expected_export = 250 * 12 * 60 / 3600 / 1000
    assert result.grid_export_kwh == pytest.approx(expected_export)
    # Export is reported but not subtracted from the import cost.
    assert result.actual_cost_dkk == pytest.approx(0.0)


def test_opposite_grid_meter_sign_is_normalised_once() -> None:
    # A meter that reports export as positive only needs one sign flip.
    accumulator = IntervalAccumulator(datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    accumulator.add(
        seconds=60,
        load_w=100,
        grid_power_w=300,
        grid_sign=-1.0,
        charge_w=0,
        discharge_w=0,
        price=2.0,
        soc=60,
        action=Action.GRID,
    )
    result = EnergyLedger().close(accumulator, degradation_cost=0.35)
    assert result.grid_import_kwh == pytest.approx(0.0)
    assert result.grid_export_kwh == pytest.approx(300 / 1000 / 60)


def test_export_totals_round_trip_through_storage() -> None:
    accumulator = IntervalAccumulator(datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    accumulator.add(
        seconds=60,
        load_w=100,
        grid_power_w=-120,
        grid_sign=1.0,
        charge_w=0,
        discharge_w=0,
        price=2.0,
        soc=60,
        action=Action.GRID,
    )
    ledger = EnergyLedger()
    ledger.close(accumulator, degradation_cost=0.0)
    restored = EnergyLedger.from_dict(ledger.as_dict())
    assert restored.total_export_kwh == pytest.approx(ledger.total_export_kwh)
    assert restored.total_export_kwh > 0.0


def test_ledger_loads_when_grid_export_field_is_missing() -> None:
    # A ledger persisted before the 1.4.0 release (no ``grid_export_kwh`` field)
    # must still load, defaulting export to zero rather than failing setup.
    legacy = {
        "intervals": [
            {
                "start": "2025-01-08T08:00:00+00:00",
                "end": "2025-01-08T08:15:00+00:00",
                "action": "battery",
                "price_dkk_per_kwh": 0.5,
                "load_kwh": 0.1,
                "grid_import_kwh": 0.0,
                "battery_charge_kwh": 0.0,
                "battery_discharge_kwh": 0.05,
                "soc_start": 60.0,
                "soc_end": 58.0,
                "baseline_cost_dkk": 0.05,
                "actual_cost_dkk": 0.025,
                "degradation_dkk": 0.0,
                "net_savings_dkk": 0.025,
                "quality": "observed",
            }
        ],
        "total_charge_kwh": 0.0,
        "total_discharge_kwh": 0.05,
        "total_export_kwh": 0.0,
        "total_net_savings_dkk": 0.025,
    }
    restored = EnergyLedger.from_dict(legacy)
    assert restored.intervals[0].grid_export_kwh == pytest.approx(0.0)
    assert "grid_export_kwh" in asdict(restored.intervals[0])


def test_calendar_period_totals_survive_restart_and_retain_previous_periods() -> None:
    local = ZoneInfo("Europe/Copenhagen")
    now = datetime(2026, 3, 2, 12, tzinfo=local)

    def interval(start: datetime, charge: float, savings: float) -> LedgerInterval:
        return LedgerInterval(
            start=start.isoformat(),
            end=start.isoformat(),
            action=Action.GRID.value,
            price_dkk_per_kwh=1,
            load_kwh=0,
            grid_import_kwh=0,
            grid_export_kwh=0,
            battery_charge_kwh=charge,
            battery_discharge_kwh=0,
            soc_start=None,
            soc_end=None,
            baseline_cost_dkk=0,
            actual_cost_dkk=0,
            degradation_dkk=0,
            net_savings_dkk=savings,
            quality="good",
        )

    ledger = EnergyLedger(
        intervals=[
            interval(datetime(2026, 2, 3, 12, tzinfo=local), 1, 10),
            interval(datetime(2026, 2, 24, 12, tzinfo=local), 2, 20),
            interval(datetime(2026, 3, 1, 12, tzinfo=local), 3, 30),
            interval(datetime(2026, 3, 2, 8, tzinfo=local), 4, 40),
        ]
    )
    restored = EnergyLedger.from_dict(ledger.as_dict())
    periods = calendar_period_bounds(now)

    totals = {
        name: restored.totals_between(*bounds) for name, bounds in periods.items()
    }

    assert totals["today"]["charge_kwh"] == 4
    assert totals["yesterday"]["net_savings_dkk"] == 30
    assert totals["week"]["charge_kwh"] == 4
    assert totals["last_week"]["net_savings_dkk"] == 50
    assert totals["month"]["net_savings_dkk"] == 70
    assert totals["last_month"]["charge_kwh"] == 3
