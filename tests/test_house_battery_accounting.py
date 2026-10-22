"""Tests for auditable interval savings and battery-use accounting."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
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


def test_battery_net_savings_work_without_household_meter_and_survive_restart() -> None:
    ledger = EnergyLedger()
    start = datetime(2026, 9, 20, 10, tzinfo=UTC)
    for index, (charge_w, discharge_w, price) in enumerate(
        [(1000, 0, 1.0), (0, 800, 3.0)]
    ):
        accumulator = IntervalAccumulator(start + timedelta(minutes=15 * index))
        for _ in range(15):
            accumulator.add(
                seconds=60,
                load_w=0,
                grid_power_w=0,
                charge_w=charge_w,
                discharge_w=discharge_w,
                price=price,
                soc=60,
                action=Action.CHARGE if charge_w else Action.BATTERY,
                flow_available=False,
            )
        ledger.close(accumulator, degradation_cost=0.35)

    restored = EnergyLedger.from_dict(ledger.as_dict())
    totals = restored.totals_since(start)
    assert totals["charge_cost_dkk"] == pytest.approx(0.25)
    assert totals["discharge_value_dkk"] == pytest.approx(0.6)
    assert totals["net_savings_dkk"] is None  # No balanced household meter.
    assert totals["battery_net_savings_dkk"] == pytest.approx(0.35)
    assert restored.total_battery_net_savings_dkk == pytest.approx(0.35)


@pytest.mark.parametrize("price", [None, 0.0, -2.0])
def test_battery_finances_distinguish_missing_zero_and_negative_tariffs(
    price: float | None,
) -> None:
    start = datetime(2026, 9, 20, 10, tzinfo=UTC)
    accumulator = IntervalAccumulator(start)
    for _ in range(15):
        accumulator.add(
            seconds=60,
            load_w=0,
            grid_power_w=0,
            charge_w=1000,
            discharge_w=0,
            price=price,
            soc=60,
            action=Action.CHARGE,
            flow_available=False,
        )
    ledger = EnergyLedger()
    ledger.close(accumulator, degradation_cost=0.35)
    restored = EnergyLedger.from_dict(ledger.as_dict())
    totals = restored.totals_since(start)
    assert totals["charge_kwh"] == pytest.approx(0.25)
    if price is None:
        assert totals["charge_cost_dkk"] is None
        assert totals["battery_net_savings_dkk"] is None
        assert restored.total_battery_net_savings_dkk is None
    else:
        assert totals["charge_cost_dkk"] == pytest.approx(0.25 * price)
        assert totals["discharge_value_dkk"] == 0
        assert totals["battery_net_savings_dkk"] == pytest.approx(-0.25 * price)
        assert restored.total_battery_net_savings_dkk == pytest.approx(-0.25 * price)


def test_empty_ledger_does_not_claim_zero_return() -> None:
    ledger = EnergyLedger.from_dict({})
    assert ledger.total_battery_net_savings_dkk is None
    assert ledger.totals_since(datetime(2026, 9, 20, tzinfo=UTC))[
        "battery_net_savings_dkk"
    ] is None


def test_lifetime_return_is_not_limited_to_retained_history() -> None:
    ledger = EnergyLedger.from_dict({
        "total_charge_cost_dkk": 12.0,
        "total_discharge_value_dkk": 32.0,
    })
    assert ledger.total_battery_net_savings_dkk == pytest.approx(20.0)
    restored = EnergyLedger.from_dict(ledger.as_dict())
    assert restored.total_battery_net_savings_dkk == pytest.approx(20.0)


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
            battery_quality="good",
            charge_cost_dkk=charge,
            discharge_value_dkk=charge + savings,
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
    for values in totals.values():
        assert values["charge_cost_dkk"] == values["charge_kwh"]
        assert values["battery_net_savings_dkk"] == values["net_savings_dkk"]
        assert values["battery_net_savings_dkk"] == pytest.approx(
            values["discharge_value_dkk"] - values["charge_cost_dkk"]
        )


@pytest.mark.parametrize("with_evidence", [True, False])
def test_coordinator_publishes_battery_finances_to_sensors(
    monkeypatch: pytest.MonkeyPatch,
    with_evidence: bool,
) -> None:
    """Battery telemetry reaches every financial entity without household flow."""
    from homeassistant.core import HomeAssistant
    import homeassistant.util.dt as dt_util
    import house_battery.coordinator as coordinator_module
    from house_battery.coordinator import Fbp1200Coordinator
    from house_battery.runtime import RuntimeState
    from house_battery.sensor import SENSORS, FbpValueSensor

    local = ZoneInfo("Europe/Copenhagen")
    now = datetime(2026, 3, 2, 12, tzinfo=local)
    ledger = EnergyLedger()
    if with_evidence:
        for day in [(2, 3), (2, 24), (3, 1), (3, 2)]:
            for minutes, charge, discharge, price in [
                (0, 1000, 0, 1),
                (15, 0, 800, 3),
            ]:
                accumulator = IntervalAccumulator(
                    datetime(2026, *day, 8, minutes, tzinfo=local)
                )
                for _ in range(15):
                    accumulator.add(
                        seconds=60,
                        load_w=0,
                        grid_power_w=0,
                        charge_w=charge,
                        discharge_w=discharge,
                        price=price,
                        soc=60,
                        action=Action.GRID,
                        flow_available=False,
                    )
                ledger.close(accumulator, degradation_cost=0.35)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - stdlib signature
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    async def scenario() -> Fbp1200Coordinator:
        hass = HomeAssistant("/tmp")
        hass.config.time_zone = "Europe/Copenhagen"
        entry = SimpleNamespace(
            entry_id="battery-finances-test",
            title="House Battery",
            data={},
            options={},
            async_on_unload=lambda callback: None,
        )
        coordinator = Fbp1200Coordinator(hass, entry)
        coordinator.runtime = RuntimeState.from_dict(
            RuntimeState(ledger=ledger).as_dict()
        )

        async def save(runtime: RuntimeState) -> None:
            del runtime

        coordinator.store = SimpleNamespace(save=save)
        coordinator.data = await coordinator._async_update_data()
        return coordinator

    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", local)
    monkeypatch.setattr(coordinator_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(coordinator_module, "get_health_problems", lambda *args: [])
    coordinator = asyncio.run(scenario())
    counts = {
        "today": 1,
        "yesterday": 1,
        "week": 1,
        "last_week": 2,
        "month": 2,
        "last_month": 2,
        "lifetime": 4,
    }
    descriptions = {item.key: item for item in SENSORS}
    for period, count in counts.items():
        for metric, expected in [
            ("charge_cost_dkk", 0.25),
            ("discharge_value_dkk", 0.6),
            ("battery_net_savings_dkk", 0.35),
        ]:
            sensor = FbpValueSensor(coordinator, descriptions[f"{period}_{metric}"])
            if with_evidence:
                assert sensor.native_value == pytest.approx(expected * count)
            else:
                assert sensor.native_value is None
            if period == "lifetime":
                assert sensor.last_reset is None
            else:
                assert sensor.last_reset == calendar_period_bounds(now)[period][0]
        if period != "lifetime":
            assert coordinator.data[period]["net_savings_dkk"] is None
