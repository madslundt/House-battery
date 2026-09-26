"""End-to-end price and forecast regressions from the September 2026 fixture."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from house_battery.planner import optimize


FIXTURE = Path(__file__).parent / "fixtures" / "prices_2026-09-25_26.json"
NOW = datetime.fromisoformat("2026-09-25T16:55:00+02:00")


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
    """Known-price arbitrage should match the supplied independent reference.

    The fixture's 110 W load, 800 W total charge-mode budget, battery limits,
    efficiency, wear, profit hurdle and switch cost are all applied together.
    The reference ranges come from the attached independent 5 Wh calculation.
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
        ("2026-09-25T17:15:00+02:00", "2026-09-25T22:00:00+02:00", Action.BATTERY),
        ("2026-09-25T22:00:00+02:00", "2026-09-26T13:15:00+02:00", Action.GRID),
        ("2026-09-26T13:15:00+02:00", "2026-09-26T14:30:00+02:00", Action.CHARGE),
        ("2026-09-26T14:30:00+02:00", "2026-09-26T17:15:00+02:00", Action.GRID),
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
    assert plan.expected_cost_dkk == pytest.approx(4.647, abs=0.20)
    assert plan.expected_savings_dkk == pytest.approx(2.235, abs=0.20)

    assert any(
        block.action is Action.CHARGE
        and block.start == datetime.fromisoformat("2026-09-26T13:15:00+02:00")
        and block.end == datetime.fromisoformat("2026-09-26T14:30:00+02:00")
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
        assert delivered_wh <= slot.expected_load_wh + settings.energy_step_wh


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
