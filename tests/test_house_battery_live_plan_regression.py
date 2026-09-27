"""Regression tests reconstructed from the 2026-09-27 live HA plan."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.dailyplan import merge_adjacent_blocks
from house_battery.forecast import extend_known_horizon
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize
from house_battery.policy import select_opportunistic_plan


FIXTURE = Path(__file__).parent / "fixtures" / "ha_plan_2026-09-27.json"


def _scenario() -> tuple[dict, list[PriceSlot], list[PriceSlot], PlannerSettings]:
    data = json.loads(FIXTURE.read_text())
    known_start = datetime.fromisoformat(data["known_start"])
    load_w = data["expected_load_w"]
    known = [
        PriceSlot(
            start=known_start + timedelta(minutes=15 * index),
            end=known_start + timedelta(minutes=15 * (index + 1)),
            price=price,
            expected_load_wh=load_w / 4,
        )
        for index, price in enumerate(data["known_prices"])
    ]
    forecast_start = datetime.fromisoformat(data["forecast_start"])
    uncertainty = data["forecast_uncertainty_dkk_per_kwh"]
    forecast = [
        PriceSlot(
            start=forecast_start + timedelta(hours=index),
            end=forecast_start + timedelta(hours=index + 1),
            price=price,
            expected_load_wh=load_w,
            source="forecast",
            uncertainty_dkk_per_kwh=uncertainty,
        )
        for index, price in enumerate(data["forecast_prices"])
    ]
    raw_settings = dict(data["settings"])
    raw_settings.pop("opportunistic_target_soc")
    return data, known, forecast, PlannerSettings(**raw_settings)


def _optimize(slots: list[PriceSlot], target_soc: float):
    data, _, _, settings = _scenario()
    configured = replace(settings, target_soc=target_soc)
    return (
        optimize(
            slots,
            now=datetime.fromisoformat(data["now"]),
            soc=data["soc"],
            settings=configured,
            current_action=Action.GRID,
        ),
        configured,
    )


def test_live_known_price_plan_charges_for_evening_and_values_carryover() -> None:
    """The known-price plan charges to its target and uses battery at the peak.

    The load value is reconstructed from HA's four published blocks; all prices
    and settings were read from their live HA entities/export on 2026-09-26.
    """
    _, known, _, _ = _scenario()
    normal, normal_settings = _optimize(known, 90)
    full, full_settings = _optimize(known, 100)

    blocks = merge_adjacent_blocks(normal.slots)
    assert [
        (block.start.hour, block.start.minute, block.end.hour, block.end.minute, block.action)
        for block in blocks[-4:]
    ] == [
        (4, 0, 13, 45, Action.GRID),
        (13, 45, 15, 45, Action.CHARGE),
        (15, 45, 17, 30, Action.GRID),
        (17, 30, 0, 0, Action.BATTERY),
    ]
    assert max(slot.soc_end for slot in normal.slots) == pytest.approx(89.89, abs=0.02)
    evening = [slot for slot in normal.slots if slot.action is Action.BATTERY]
    assert sum(slot.expected_load_wh for slot in evening) == pytest.approx(965.9)
    assert sum(slot.battery_charge_wh for slot in normal.slots) == pytest.approx(1365)

    # The known evening peak justifies the optional higher charge ceiling.
    decision = select_opportunistic_plan(
        normal,
        full,
        normal_settings,
        full_settings,
        enabled=True,
    )
    assert decision.active
    assert decision.settings.target_soc == 100


def test_live_forecast_extension_supports_a_full_charge_when_objective_improves() -> None:
    """Select the higher target when forecast-adjusted cost supports it."""
    data, known, forecast, _ = _scenario()
    extended = extend_known_horizon(
        known,
        forecast,
        now=datetime.fromisoformat(data["now"]),
        uncertainty_dkk_per_kwh=data["forecast_uncertainty_dkk_per_kwh"],
    )
    normal, normal_settings = _optimize(extended, 90)
    full, full_settings = _optimize(extended, 100)

    assert max(slot.soc_end for slot in normal.slots) == pytest.approx(89.89, abs=0.02)
    assert max(slot.soc_end for slot in full.slots) == pytest.approx(99.85, abs=0.02)
    assert full.realized_savings_dkk - normal.realized_savings_dkk == pytest.approx(
        0.2860, abs=0.001
    )
    assert any(slot.action is Action.BATTERY for slot in normal.slots if slot.price_source == "forecast")

    decision = select_opportunistic_plan(
        normal,
        full,
        normal_settings,
        full_settings,
        enabled=True,
    )
    assert decision.active
    assert "Risk-adjusted plan objective" in decision.reason
    assert decision.settings.target_soc == 100
