"""Tests for auditable interval savings and battery-use accounting."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.accounting import EnergyLedger, IntervalAccumulator
from house_battery.models import Action


def test_complete_battery_interval_books_net_savings_after_wear() -> None:
    accumulator = IntervalAccumulator(datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    for _ in range(15):
        accumulator.add(
            seconds=60,
            load_w=500,
            grid_import_w=0,
            grid_export_w=0,
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
        grid_import_w=0,
        grid_export_w=0,
        charge_w=0,
        discharge_w=500,
        price=2.0,
        soc=80,
        action=Action.BATTERY,
    )
    result = EnergyLedger().close(accumulator, degradation_cost=0.35)
    assert result.quality == "incomplete"
    assert result.net_savings_dkk is None
