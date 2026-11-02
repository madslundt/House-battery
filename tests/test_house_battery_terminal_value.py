"""Uncertain continuation must not manufacture profitable battery movement."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import sqrt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.capabilities import ControlCapabilities
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize

NOW = datetime(2026, 3, 9, tzinfo=UTC)
HOUR = timedelta(hours=1)


def settings(**changes) -> PlannerSettings:
    return replace(PlannerSettings(
        capacity_wh=2000, reserve_soc=20, target_soc=90,
        charge_power_w=800, discharge_power_w=800,
        round_trip_efficiency=.85, degradation_cost_dkk_per_kwh=.35,
        minimum_profit_dkk_per_kwh=.75, switching_penalty_dkk=.05,
        energy_step_wh=5,
        control_capabilities=ControlCapabilities.direct_local(mains_isolated=True),
    ), **changes)


def horizon(known_price=3, forecast_price=3, uncertainty=.25, *, load_w=100):
    return [PriceSlot(NOW + HOUR * i, NOW + HOUR * (i + 1),
                      known_price if i < 12 else forecast_price, load_w,
                      source="known" if i < 12 else "forecast",
                      uncertainty_dkk_per_kwh=0 if i < 12 else uncertainty)
            for i in range(72)]


@pytest.mark.parametrize("uncertainty", [.25, 1, 6])
@pytest.mark.parametrize("profit", [0, .75])
def test_flat_prices_never_liquidate_existing_stock_because_of_uncertainty(uncertainty, profit):
    plan = optimize(horizon(uncertainty=uncertainty), now=NOW, soc=90,
                    settings=settings(minimum_profit_dkk_per_kwh=profit))

    assert all(slot.action is Action.GRID for slot in plan.slots)
    assert plan.battery_throughput_kwh == 0
    assert plan.slots[-1].soc_end == 90


def test_flat_known_and_forecast_prices_preserve_cash_and_inventory():
    inputs = horizon()
    assert len([slot for slot in inputs if slot.source == "forecast"]) == 60
    plan = optimize(inputs, now=NOW, soc=90, settings=settings())
    prefix = plan.slots[:12]

    # A flat 3 DKK tariff costs 12 * 0.1 kWh * 3 = 3.60. Any depletion,
    # conversion losses and switching would make restoring inventory cost more.
    missing_wh = 2000 * (.9 - prefix[-1].soc_end / 100)
    common_cost = sum(slot.interval_cost_dkk for slot in prefix) + missing_wh / sqrt(.85) / 1000 * 3
    assert common_cost == pytest.approx(3.60)
    assert plan.battery_throughput_kwh == 0


def test_credible_future_decline_still_uses_existing_energy_at_high_known_prices():
    plan = optimize(horizon(forecast_price=1, uncertainty=.25),
                    now=NOW, soc=90, settings=settings())

    assert plan.current_action is Action.BATTERY
    assert any(slot.battery_discharge_wh > 0 for slot in plan.slots[:12])


@pytest.mark.parametrize("soc", [20, 50])
def test_uncertain_high_future_prices_do_not_create_speculative_charging(soc):
    plan = optimize(horizon(forecast_price=9, uncertainty=8),
                    now=NOW, soc=soc, settings=settings())

    assert all(slot.action is Action.GRID for slot in plan.slots)
    assert plan.battery_throughput_kwh == 0


def test_credible_high_future_prices_still_allow_preparation_at_known_low_prices():
    inputs = [replace(slot, price=1) if index >= 24 else slot
              for index, slot in enumerate(horizon(known_price=1, forecast_price=5))]
    plan = optimize(inputs, now=NOW, soc=20, settings=settings())

    assert any(slot.action is Action.CHARGE for slot in plan.slots[:12])
    assert any(slot.action is Action.BATTERY for slot in plan.slots[12:])


def test_negative_known_prices_still_allow_profitable_charging():
    plan = optimize(horizon(known_price=-1, forecast_price=-1, uncertainty=6),
                    now=NOW, soc=20, settings=settings())

    assert any(slot.action is Action.CHARGE for slot in plan.slots[:12])
    assert plan.realized_savings_dkk > 0


def test_uncertainty_cannot_create_new_charge_credit_in_an_already_flat_market():
    plan = optimize(horizon(known_price=1, forecast_price=1, uncertainty=6),
                    now=NOW, soc=20, settings=settings())

    assert all(slot.action is Action.GRID for slot in plan.slots)
    assert plan.battery_throughput_kwh == 0


def test_explicit_storage_floor_remains_an_explicit_replay_input():
    plan = optimize(horizon(), now=NOW, soc=90, settings=settings(),
                    terminal_price_floor_dkk_per_kwh=4)

    assert plan.terminal_price_floor_dkk_per_kwh == 4
    assert plan.terminal_price_dkk_per_kwh >= 4
    assert plan.terminal_inventory_price_dkk_per_kwh >= 4
