"""Tests for auditable interval savings and battery-use accounting."""

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.json import json_dumps

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.accounting import (
    EnergyLedger,
    IntervalAccumulator,
    LedgerInterval,
    calendar_period_bounds,
)
from house_battery.models import Action


@pytest.mark.parametrize("case,expected_charge", [
    ("standby", 0), ("unit_charging", 48), ("incomplete_unit", 48),
    ("charge_controls", 48), ("incomplete_controls", 48),
])
def test_verified_idle_standby_input_is_not_booked_as_battery_charge(
    monkeypatch, case: str, expected_charge: float
) -> None:
    import house_battery.coordinator as coordinator_module
    from test_house_battery_cutoff_gate import coordinator
    from test_house_battery_safety_regression import IdleDevice

    start = datetime(2026, 10, 29, 10, tzinfo=UTC)

    class Clock(datetime):
        tick = start

        @classmethod
        def now(cls, tz=None):
            return cls.tick.astimezone(tz) if tz else cls.tick.replace(tzinfo=None)

    async def scenario() -> None:
        coord, _, _, _ = await coordinator(now=start)
        coord.runtime.execution_enabled = False
        device = IdleDevice(per_unit_charge=48 if case == "unit_charging" else 0)
        if case == "incomplete_unit":
            device.charge = None
        elif case == "charge_controls":
            device.controls["3003"] = "1,00:00,23:59,-48,0,6,5,0,0,90,10"
        elif case == "incomplete_controls":
            del device.controls["3018"]
        coord.local_client = device
        for minute in range(16):
            Clock.tick = start + timedelta(minutes=minute)
            status = await coord._async_update_data()

        assert coord._local_snapshot.raw_reported_charge_power_w == 48
        assert coord.runtime.ledger.total_charge_kwh == pytest.approx(expected_charge * 14 / 60 / 1000)
        assert coord.runtime.ledger.total_charge_cost_dkk == pytest.approx(expected_charge * 14 / 60 / 1000 * 4)
        assert sum(block["charge_kwh"] for block in status["actual_history_blocks"]) == pytest.approx(expected_charge * 15 / 60 / 1000)
        assert status["battery_charge_power_w"] == expected_charge
        assert status["power_source"] == ("grid" if expected_charge == 0 else "charging")

    monkeypatch.setattr(coordinator_module, "datetime", Clock)
    asyncio.run(scenario())


@pytest.mark.parametrize("old_price", [float("nan"), float("inf"), -float("inf")])
def test_legacy_nonfinite_tariffs_keep_energy_and_resume_after_reload(old_price: float) -> None:
    from house_battery.evidence import EvidenceCollector
    from house_battery.runtime import RuntimeState

    start = datetime(2026, 10, 29, 10, tzinfo=UTC)
    old_interval = IntervalAccumulator(start)
    for _ in range(15):
        old_interval.add(
            seconds=60, load_w=1000, grid_power_w=0, charge_w=0,
            discharge_w=1000, price=old_price, soc=50, action=Action.BATTERY,
        )
    ledger = EnergyLedger()
    ledger.close(old_interval, 0.35)
    payload = json.loads(json_dumps(RuntimeState(ledger=ledger).as_dict()))
    restored = RuntimeState.from_dict(payload)

    assert restored.ledger.total_discharge_kwh == pytest.approx(0.25)
    assert restored.ledger.total_battery_net_savings_dkk is None
    assert restored.ledger.savings_intervals == 0

    async def scenario() -> None:
        async def save() -> None:
            pass

        observer = EvidenceCollector(lambda: restored, lambda: None, save, lambda: None)
        for minute in range(30, 46):
            await observer.async_observe(
                start + timedelta(minutes=minute), 2, Action.CHARGE, 50,
                battery_charge_power_w=1000, battery_discharge_power_w=0,
                battery_learning_enabled=False,
            )

        assert restored.ledger.total_discharge_kwh == pytest.approx(0.25)
        assert restored.ledger.total_charge_kwh == pytest.approx(14 / 60)
        assert restored.ledger.total_charge_cost_dkk == pytest.approx(14 / 30)
        assert restored.ledger.total_battery_net_savings_dkk == pytest.approx(-14 / 30)

    asyncio.run(scenario())


def test_repairing_unknown_ledger_amounts_preserves_valid_negative_tariff_evidence() -> None:
    from house_battery.runtime import RuntimeState

    start = datetime(2026, 10, 29, 10, tzinfo=UTC)
    ledger = EnergyLedger()
    for index, price in enumerate([-1, float("nan")]):
        accumulator = IntervalAccumulator(start + timedelta(minutes=15 * index))
        for _ in range(15):
            accumulator.add(
                seconds=60, load_w=1000, grid_power_w=2000, charge_w=1000,
                discharge_w=0, price=price, soc=50, action=Action.CHARGE,
            )
        ledger.close(accumulator, 0.35)

    restored = RuntimeState.from_dict(
        json.loads(json_dumps(RuntimeState(ledger=ledger).as_dict()))
    ).ledger

    assert restored.total_charge_kwh == pytest.approx(0.5)
    assert restored.total_charge_cost_dkk == pytest.approx(-0.25)
    assert restored.charge_cost_intervals == 1
    assert restored.total_net_savings_dkk == pytest.approx(0.25)
    assert restored.savings_intervals == 1


@pytest.mark.parametrize("resume_minute,expected_kwh", [(13, 13 / 60), (20, 0.2)])
def test_open_accounting_interval_survives_restart_without_filling_offline_time(
    resume_minute: int, expected_kwh: float
) -> None:
    from house_battery.evidence import EvidenceCollector
    from house_battery.runtime import RuntimeState

    async def scenario() -> None:
        runtime = RuntimeState()

        async def save() -> None:
            pass

        def collector() -> EvidenceCollector:
            return EvidenceCollector(lambda: runtime, lambda: None, save, lambda: None)

        observer = collector()
        start = datetime(2026, 10, 29, 10, tzinfo=UTC)

        async def observe(minute: int) -> None:
            await observer.async_observe(
                start + timedelta(minutes=minute), 1, Action.CHARGE, 50,
                battery_charge_power_w=1000, battery_discharge_power_w=0,
                battery_learning_enabled=False,
            )

        for minute in range(13):
            await observe(minute)
        runtime = RuntimeState.from_dict(runtime.as_dict())
        observer = collector()
        for minute in range(resume_minute, max(16, resume_minute + 1)):
            await observe(minute)

        assert runtime.ledger.total_charge_kwh == pytest.approx(expected_kwh)
        assert len(runtime.ledger.intervals) == 1
        assert runtime.ledger.intervals[0].battery_quality == "good"

    asyncio.run(scenario())


def test_unavailable_measurements_do_not_bridge_an_accounting_gap() -> None:
    from house_battery.evidence import EvidenceCollector
    from house_battery.runtime import RuntimeState

    async def scenario() -> None:
        runtime = RuntimeState()

        async def save() -> None:
            pass

        observer = EvidenceCollector(lambda: runtime, lambda: None, save, lambda: None)
        start = datetime(2026, 10, 29, 10, tzinfo=UTC)
        for minute, valid in [(0, True), (1, True), (2, False), (3, True), (4, True), (15, False)]:
            await observer.async_observe(
                start + timedelta(minutes=minute), 1, Action.CHARGE,
                50 if valid else None,
                battery_charge_power_w=1000 if valid else None,
                battery_discharge_power_w=0 if valid else None,
                battery_learning_enabled=False,
            )

        # Only 00->01 and 03->04 had fresh measurements at both endpoints.
        assert runtime.ledger.intervals[0].battery_charge_kwh == pytest.approx(1 / 30)

    asyncio.run(scenario())


def test_battery_learning_discards_sessions_crossing_missing_balanced_flow() -> None:
    from house_battery.evidence import EvidenceCollector
    from house_battery.models import derive_power_flow
    from house_battery.runtime import RuntimeState

    async def scenario() -> None:
        runtime = RuntimeState()
        flow = None

        async def save() -> None:
            pass

        observer = EvidenceCollector(lambda: runtime, lambda: flow, save, lambda: None)
        start = datetime(2026, 10, 29, 10, tzinfo=UTC)
        for minute in range(13):
            soc = 80 - minute
            flow = derive_power_flow(soc, 1000, 0)
            await observer.async_observe(start + timedelta(minutes=minute), 1, Action.BATTERY, soc)
        flow = None
        await observer.async_observe(start + timedelta(minutes=13), 1, Action.BATTERY, None)
        flow = derive_power_flow(50, 1000, 1000)
        await observer.async_observe(start + timedelta(minutes=14), 1, Action.GRID, 50)

        assert runtime.battery_learner.capacity_samples_wh == []

    asyncio.run(scenario())


def test_battery_accounting_continues_while_household_meter_recovers() -> None:
    from house_battery.evidence import EvidenceCollector
    from house_battery.models import derive_power_flow
    from house_battery.runtime import RuntimeState

    async def scenario() -> None:
        runtime = RuntimeState()
        flow = None

        async def save() -> None:
            pass

        observer = EvidenceCollector(lambda: runtime, lambda: flow, save, lambda: None)
        start = datetime(2026, 10, 29, 10, tzinfo=UTC)
        for minute in range(5):
            flow = None if minute == 2 else derive_power_flow(50, 1000, 0)
            await observer.async_observe(
                start + timedelta(minutes=minute), 1, Action.BATTERY, 50,
                battery_charge_power_w=0, battery_discharge_power_w=1000,
                battery_learning_enabled=False,
            )
        flow = None
        await observer.async_observe(
            start + timedelta(minutes=15), 1, Action.BATTERY, None,
            battery_learning_enabled=False,
        )

        interval = runtime.ledger.intervals[0]
        assert interval.battery_discharge_kwh == pytest.approx(1 / 15)
        assert interval.load_kwh == pytest.approx(1 / 30)

    asyncio.run(scenario())


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
    import homeassistant.util.dt as dt_util
    import house_battery.coordinator as coordinator_module
    from homeassistant.core import HomeAssistant
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
        def now(cls, tz=None):
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
