"""Tests for auditable interval savings and battery-use accounting."""

import sys
from datetime import UTC, datetime
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
            grid_import_w=0,
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
        charge_w=0,
        discharge_w=500,
        price=2.0,
        soc=80,
        action=Action.BATTERY,
    )
    result = EnergyLedger().close(accumulator, degradation_cost=0.35)
    assert result.quality == "incomplete"
    assert result.net_savings_dkk is None


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
