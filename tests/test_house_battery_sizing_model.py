"""Closed-SOC sizing counterfactuals have no free initial or terminal energy."""

import itertools
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings, PriceSlot
from house_battery.sizing_model import simulate_storage

BASE = datetime(2026, 10, 1, tzinfo=UTC)


def _settings(**changes) -> PlannerSettings:
    values = {"capacity_wh": 1000, "reserve_soc": 0, "target_soc": 100,
              "charge_power_w": 1000, "discharge_power_w": 800,
              "round_trip_efficiency": 1, "degradation_cost_dkk_per_kwh": 0,
              "minimum_profit_dkk_per_kwh": 0, "switching_penalty_dkk": 0}
    values.update(changes)
    return PlannerSettings(**values)


def _slots(prices: list[float], loads: list[float] | None = None) -> list[PriceSlot]:
    return [PriceSlot(BASE + timedelta(hours=i), BASE + timedelta(hours=i + 1),
                      price, expected_load_wh=(loads or [200] * len(prices))[i])
            for i, price in enumerate(prices)]


def _integer_oracle(prices, loads, capacity, charge_power, discharge_power, wear=0):
    # Independent exhaustive integer-unit control enumeration. With eta=1
    # and these integral capacities, the continuous positive-price optimum
    # has integral vertices; only same-hour opposing modes are prohibited.
    layer = {0: 0.0}
    for price, load in zip(prices, loads, strict=True):
        next_layer = {}
        for stored, gain in layer.items():
            for charge in range(min(charge_power - load, capacity - stored) + 1):
                end = stored + charge
                next_layer[end] = max(next_layer.get(end, -float("inf")),
                                      gain - charge * price / 1000)
            for discharge in range(min(stored, load, discharge_power) + 1):
                end = stored - discharge
                next_layer[end] = max(next_layer.get(end, -float("inf")),
                                      gain + discharge * (price - wear) / 1000)
        layer = next_layer
    return layer[0]


@pytest.mark.parametrize("price_levels,wear", [([.5, 2, 4], 0), ([-2, 0, 2], 0), ([.5, 2, 4], .25)])
def test_small_cases_match_independent_exhaustive_oracle(price_levels, wear) -> None:
    for prices in itertools.product(price_levels, repeat=4):
        loads = [1, 1, 1, 1]
        settings = _settings(capacity_wh=3, charge_power_w=3, discharge_power_w=2,
                              degradation_cost_dkk_per_kwh=wear)
        expected = _integer_oracle(prices, loads, 3, 3, 2, wear)
        actual = simulate_storage(_slots(list(prices), loads), settings)
        assert actual["net_savings_dkk"] == pytest.approx(expected, abs=1e-9)


def test_first_peak_and_last_dip_cannot_use_free_boundary_energy() -> None:
    for prices in ([8], [8, 1], [8, 4, 1]):
        result = simulate_storage(_slots(list(prices)), _settings())
        assert result["net_savings_dkk"] == 0
        assert result["charge_kwh"] == result["discharge_kwh"] == 0


def test_more_capacity_increases_energy_but_never_increases_power_limits() -> None:
    slots = _slots([1, 4, 4], [0, 200, 200])
    small = simulate_storage(slots, _settings(capacity_wh=200))
    large = simulate_storage(slots, _settings(capacity_wh=400))
    assert small["discharge_kwh"] == pytest.approx(.2)
    assert large["discharge_kwh"] == pytest.approx(.4)
    assert small["net_savings_dkk"] == pytest.approx(.6)
    assert large["net_savings_dkk"] == pytest.approx(1.2)


def test_charge_budget_accounts_for_connected_load() -> None:
    result = simulate_storage(_slots([1, 4]), _settings(charge_power_w=300))
    assert result["charge_kwh"] == pytest.approx(.1)
    assert result["discharge_kwh"] == pytest.approx(.1)
    assert result["net_savings_dkk"] == pytest.approx(.3)


def test_usable_capacity_respects_reserve_and_target() -> None:
    result = simulate_storage(_slots([1, 4, 4, 4], [0, 250, 250, 250]),
                              _settings(reserve_soc=20, target_soc=80))
    assert result["peak_stored_wh"] <= 600 + 1e-6
    assert result["discharge_kwh"] == pytest.approx(.6)


def test_energy_can_be_carried_across_a_weekend_without_terminal_credit() -> None:
    prices, loads = [1] + [2] * 47 + [6], [0] * 48 + [250]
    result = simulate_storage(_slots(prices, loads), _settings(capacity_wh=500))
    assert result["discharge_kwh"] == pytest.approx(.25)
    assert result["net_savings_dkk"] == pytest.approx(1.25)
    assert result["modeled_hours"] == 49


def test_a_missing_history_interval_breaks_storage_carryover() -> None:
    slots = _slots([1, 4])
    assert simulate_storage(slots, _settings())["net_savings_dkk"] > 0
    slots[1] = replace(slots[1], start=slots[1].start + timedelta(hours=1),
                       end=slots[1].end + timedelta(hours=1))
    result = simulate_storage(slots, _settings())
    assert result["net_savings_dkk"] == 0
    assert result["segments"] == 2


def test_losses_close_the_energy_balance_in_ac_units() -> None:
    result = simulate_storage(_slots([.1, 5, 5], [0, 400, 400]),
                              _settings(round_trip_efficiency=.81))
    assert result["discharge_kwh"] == pytest.approx(result["charge_kwh"] * .81)
    assert abs(result["stored_energy_balance_wh"]) < 1e-6
    assert result["peak_stored_wh"] <= 1000 + 1e-6


def test_wear_can_make_an_apparent_price_spread_unprofitable() -> None:
    result = simulate_storage(_slots([1, 1.2]),
                              _settings(degradation_cost_dkk_per_kwh=.5))
    assert result["charge_kwh"] == result["discharge_kwh"] == 0
    assert result["net_savings_dkk"] == 0


def test_switching_cost_is_reported_posthoc_and_negative_net_uses_grid_only() -> None:
    result = simulate_storage(_slots([1, 4], [0, 200]),
                              _settings(switching_penalty_dkk=.05))
    assert result["bill_savings_dkk"] > result["net_savings_dkk"] > 0
    assert result["switching_cost_dkk"] > 0
    assert "posthoc" in result["optimization"]
    tiny = simulate_storage(_slots([1, 1.01], [0, 1]),
                            _settings(switching_penalty_dkk=.05))
    assert tiny["net_savings_dkk"] == 0
    assert tiny["charge_kwh"] == tiny["discharge_kwh"] == 0


def test_negative_prices_repair_shared_hour_time_without_inventing_energy() -> None:
    slots = _slots([-5, -2, -1, 6], [0, 500, 500, 500])
    result = simulate_storage(slots, _settings(capacity_wh=1000,
        round_trip_efficiency=.64, charge_power_w=1500))
    assert result["net_savings_dkk"] >= 0
    assert result["maximum_hour_utilization"] <= 1 + 1e-9
    assert result["repaired_hours"] > 0
    assert abs(result["stored_energy_balance_wh"]) < 1e-6
    assert result["discharge_kwh"] == pytest.approx(result["charge_kwh"] * .64)
    assert result["active_hours"] <= result["modeled_hours"] + 1e-9


def test_no_discharge_path_and_unpriced_rows_fail_closed() -> None:
    slots = _slots([1, 4])
    assert simulate_storage(slots, _settings(discharge_enabled=False))["charge_kwh"] == 0
    slots[0] = replace(slots[0], price=float("nan"))
    result = simulate_storage(slots, _settings())
    assert result["net_savings_dkk"] == 0
    assert result["modeled_hours"] == 1
