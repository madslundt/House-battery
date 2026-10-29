"""End-to-end price and forecast regressions from the September 2026 fixture."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import (
    CONF_LOAD_POWER,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITIES,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.dailyplan import merge_adjacent_blocks
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import _future_slots, optimize
from house_battery.price import normalize_price_rows_with_report

FIXTURE = Path(__file__).parent / "fixtures" / "prices_2026-09-25_26.json"
NOW = datetime.fromisoformat("2026-09-25T16:55:00+02:00")


@pytest.mark.parametrize("start,end,price", [
    ("2026-10-29T19:00:00", "2026-10-29T20:00:00+01:00", 1),
    ("2026-10-29T19:00:00+01:00", "2026-10-29T20:00:00", 1),
    ("2026-10-29T19:00:00+01:00", "2026-10-29T20:00:00+01:00", "nan"),
    ("2026-10-29T19:00:00+01:00", "2026-10-29T19:00:00+01:00", 1),
])
def test_malformed_price_rows_are_rejected_without_aborting_valid_feed(start, end, price):
    valid = {"start": "2026-10-29T20:00:00+01:00",
             "end": "2026-10-29T21:00:00+01:00", "price": 2}
    result = normalize_price_rows_with_report([
        {"start": start, "end": end, "price": price}, valid,
    ])
    assert result.invalid_rows == 1
    assert len(result.slots) == 1
    assert result.slots[0].price == 2


def test_malformed_price_feed_restores_grid_instead_of_aborting_refresh():
    from test_house_battery_cutoff_gate import coordinator

    async def scenario():
        coord, client, hass, _ = await coordinator()
        now = datetime.now(UTC)
        hass.states.async_set("sensor.prices", "2", {"prices": [{
            "start": now.replace(tzinfo=None).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(), "price": 2,
        }]})
        status = await coord._async_update_data()
        assert status["system_state"] != "ACTIVE"
        assert ("grid_idle", 20, 90) in client.calls
        assert hass.states.get("switch.grid_input").state == "on"

    asyncio.run(scenario())


def _known_prices() -> list[PriceSlot]:
    rows = json.loads(FIXTURE.read_text())
    return [
        PriceSlot(
            datetime.fromisoformat(row["start"]),
            datetime.fromisoformat(row["end"]),
            row["price"],
            expected_load_wh=110 * (
                datetime.fromisoformat(row["end"])
                - datetime.fromisoformat(row["start"])
            ).total_seconds()
            / 3600,
        )
        for row in rows
    ]


def _settings(**overrides: float) -> PlannerSettings:
    values: dict[str, float] = {
        "capacity_wh": 1956,
        "reserve_soc": 20,
        "target_soc": 90,
        "charge_power_w": 800,
        "discharge_power_w": 800,
        "round_trip_efficiency": 0.85,
        "degradation_cost_dkk_per_kwh": 0.35,
        "minimum_profit_dkk_per_kwh": 0.75,
        "switching_penalty_dkk": 0.05,
        "energy_step_wh": 5,
    }
    values.update(overrides)
    return PlannerSettings(**values)


def test_known_price_regression_uses_load_wear_and_profit_hurdle() -> None:
    """Known-price arbitrage stays feasible with conservative 5 Wh movements.

    The fixture's 110 W load, 800 W total charge-mode budget, battery limits,
    efficiency, wear, profit hurdle and switch cost are all applied together.
    Movement is rounded toward zero so it cannot exceed load or power budgets.
    """
    plan = optimize(
        _known_prices(),
        now=NOW,
        soc=48,
        settings=_settings(),
        current_action=Action.GRID,
    )
    blocks = merge_adjacent_blocks(plan.slots)
    actions = [(block.start, block.end, block.action) for block in blocks]
    expected_actions = [
        ("2026-09-25T16:55:00+02:00", "2026-09-25T17:15:00+02:00", Action.GRID),
        ("2026-09-25T17:15:00+02:00", "2026-09-25T22:45:00+02:00", Action.BATTERY),
        ("2026-09-25T22:45:00+02:00", "2026-09-26T12:15:00+02:00", Action.GRID),
        ("2026-09-26T12:15:00+02:00", "2026-09-26T14:45:00+02:00", Action.CHARGE),
        ("2026-09-26T14:45:00+02:00", "2026-09-26T17:15:00+02:00", Action.GRID),
        ("2026-09-26T17:15:00+02:00", "2026-09-27T00:00:00+02:00", Action.BATTERY),
    ]
    assert actions == [
        (datetime.fromisoformat(start), datetime.fromisoformat(end), action)
        for start, end, action in expected_actions
    ]
    for previous, current in zip(plan.slots, plan.slots[1:]):
        assert previous.start < previous.end
        assert previous.end == current.start

    assert sum(slot.expected_load_wh for slot in plan.slots) == pytest.approx(3419.17, abs=1)
    assert plan.baseline_cost_dkk == pytest.approx(6.8816, abs=0.02)
    assert plan.expected_cost_dkk == pytest.approx(5.2765, abs=0.02)
    assert plan.expected_savings_dkk == pytest.approx(2.3507, abs=0.02)
    assert plan.optimization_objective_dkk < 5.374505  # Full-power-only reference.

    assert any(
        block.action is Action.CHARGE
        and block.start == datetime.fromisoformat("2026-09-26T12:15:00+02:00")
        and block.end == datetime.fromisoformat("2026-09-26T14:45:00+02:00")
        for block in blocks
    )
    assert any(
        block.action is Action.BATTERY
        and block.start <= datetime.fromisoformat("2026-09-26T17:15:00+02:00")
        and block.end >= datetime.fromisoformat("2026-09-26T23:45:00+02:00")
        for block in blocks
    )

    settings = _settings()
    for slot in plan.slots:
        assert slot.soc_start >= settings.reserve_soc - 0.1
        assert slot.soc_end >= settings.reserve_soc - 0.1
        assert slot.soc_end <= settings.target_soc + 0.1
        assert slot.grid_import_wh >= 0
        delivered_wh = slot.battery_discharge_wh * (0.85**0.5)
        hours = (slot.end - slot.start).total_seconds() / 3600
        assert delivered_wh <= min(slot.expected_load_wh, settings.discharge_power_w * hours) + 1e-8
        assert slot.battery_charge_wh / (0.85**0.5) <= max(
            0, settings.charge_power_w * hours - slot.expected_load_wh
        ) + 1e-8
        assert (slot.soc_end - slot.soc_start) * settings.capacity_wh / 100 == pytest.approx(
            slot.battery_charge_wh - slot.battery_discharge_wh
        )


def test_enabled_forecast_extends_optimizer_with_uncertainty() -> None:
    """An enabled contiguous forecast changes the executable optimized horizon."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    known_rows = [
        {
            "start": (now + timedelta(hours=index)).isoformat(),
            "end": (now + timedelta(hours=index + 1)).isoformat(),
            "price": 2.0,
        }
        for index in range(2)
    ]
    forecast_rows = [
        {
            "start": (now + timedelta(hours=2 + index)).isoformat(),
            "end": (now + timedelta(hours=3 + index)).isoformat(),
            "price": price,
        }
        for index, price in enumerate((0.5, 0.5, 4.0, 4.0))
    ]

    async def make_plan(include_forecast: bool):
        hass = HomeAssistant("/tmp")
        entry_data = {CONF_PRICE_ENTITIES: ["sensor.known"], CONF_LOAD_POWER: "sensor.load"}
        if include_forecast:
            entry_data[CONF_PRICE_FORECAST_ENTITIES] = ["sensor.forecast"]
        entry = type(
            "Entry",
            (),
            {
                "entry_id": "forecast-regression",
                "title": "test",
                "data": entry_data,
                "options": {},
                "async_on_unload": lambda self, callback: None,
            },
        )()
        coordinator = Fbp1200Coordinator(hass, entry)
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.known", "2.0", {"prices": known_rows})
        hass.states.async_set("sensor.load", "300")
        if include_forecast:
            hass.states.async_set(
                "sensor.forecast",
                "2026-09-25T10:00:00+02:00",
                {"prices": forecast_rows},
            )
        price_slots = coordinator._price_slots(now)
        optimized = optimize(
            price_slots,
            now=now,
            soc=20,
            settings=_settings(),
            current_action=Action.GRID,
        )
        return price_slots, optimized, coordinator._forecast_plan

    known_slots, plan_without_forecast, _ = asyncio.run(make_plan(False))
    forecast_slots, plan_with_forecast, guidance = asyncio.run(make_plan(True))

    assert forecast_slots[: len(known_slots)] == known_slots
    assert [slot.source for slot in forecast_slots[len(known_slots) :]] == [
        "forecast"
    ] * 4
    assert all(
        slot.uncertainty_dkk_per_kwh == pytest.approx(0.25)
        for slot in forecast_slots[len(known_slots) :]
    )
    assert len(plan_with_forecast.slots) == len(plan_without_forecast.slots) + 4
    assert any(
        slot.action is Action.CHARGE and slot.price_source == "forecast"
        for slot in plan_with_forecast.slots
    )
    assert any(
        slot.action is Action.BATTERY and slot.price_source == "forecast"
        for slot in plan_with_forecast.slots
    )
    assert guidance is not None
    assert {block.recommendation for block in guidance.blocks} == {
        Action.CHARGE,
        Action.BATTERY,
    }


def _fall_back_price_rows(*, explicit_ends: bool) -> list[dict]:
    """Match native HA datetimes on Copenhagen's 25-hour autumn day."""
    local = ZoneInfo("Europe/Copenhagen")
    start = datetime(2026, 10, 25, tzinfo=local).astimezone(UTC)
    rows = []
    for index in range(100):
        interval_start = (start + timedelta(minutes=15 * index)).astimezone(local)
        interval_end = (start + timedelta(minutes=15 * (index + 1))).astimezone(local)
        hour = interval_start.hour
        price = .3 if hour < 6 else 4.0 if 17 <= hour < 21 else .5
        row = {"start": interval_start, "price": price}
        if explicit_ends:
            row["end"] = interval_end
        rows.append(row)
    return rows


@pytest.mark.parametrize("explicit_ends", [False, True])
def test_native_fall_back_prices_preserve_both_folds_and_quarter_hours(explicit_ends):
    report = normalize_price_rows_with_report(
        _fall_back_price_rows(explicit_ends=explicit_ends)
    )
    assert report.source_rows == 100
    assert report.invalid_rows == 0
    assert len(report.slots) == 100
    assert all(slot.hours == .25 for slot in report.slots)
    assert all(
        previous.end == current.start
        for previous, current in zip(report.slots, report.slots[1:])
    )
    local = ZoneInfo("Europe/Copenhagen")
    repeated_hour = [
        slot.start.astimezone(local)
        for slot in report.slots
        if slot.start.astimezone(local).hour == 2
    ]
    assert [start.fold for start in repeated_hour] == [0] * 4 + [1] * 4
    assert len({start.timestamp() for start in repeated_hour}) == 8


def test_native_fall_back_horizon_reaches_peak_and_plans_cheap_charging():
    rows = _fall_back_price_rows(explicit_ends=True)
    report = normalize_price_rows_with_report(rows)
    slots = [replace(slot, expected_load_wh=222 * .25) for slot in report.slots]
    now = datetime(2026, 10, 24, 22, tzinfo=UTC)
    horizon_end = datetime(2026, 10, 25, 23, tzinfo=UTC)
    future = _future_slots(slots, now)
    assert len(future) == 100
    assert future[-1].end == horizon_end

    plan = optimize(
        slots, now=now, soc=19,
        settings=_settings(reserve_soc=10), current_action=Action.GRID,
    )
    assert len(plan.slots) == 100
    assert plan.slots[-1].end == horizon_end
    cheap_charges = [
        slot for slot in plan.slots
        if slot.action is Action.CHARGE and slot.price == .3
    ]
    peak_discharges = [
        slot for slot in plan.slots
        if slot.action is Action.BATTERY and slot.price == 4.0
    ]
    assert cheap_charges
    assert peak_discharges
    assert cheap_charges[-1].end < peak_discharges[0].start
