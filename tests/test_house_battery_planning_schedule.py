"""Economic planning follows market changes while telemetry can keep polling."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings, PriceSlot
from house_battery.planning_schedule import (
    earliest_future_boundary,
    planning_change_reason,
    planning_key,
)

BASE = datetime(2026, 11, 2, 12, tzinfo=UTC)


def settings() -> PlannerSettings:
    return PlannerSettings(
        capacity_wh=2000, reserve_soc=20, target_soc=90,
        charge_power_w=1200, discharge_power_w=800,
        round_trip_efficiency=.85, degradation_cost_dkk_per_kwh=.35,
        minimum_profit_dkk_per_kwh=.75, switching_penalty_dkk=.05,
    )


def prices() -> list[PriceSlot]:
    return [
        PriceSlot(BASE, BASE + timedelta(minutes=15), 1, 25),
        PriceSlot(BASE + timedelta(minutes=15), BASE + timedelta(minutes=30), 1, 25),
        PriceSlot(BASE + timedelta(minutes=30), BASE + timedelta(hours=1), 3, 50,
                  source="forecast", uncertainty_dkk_per_kwh=.25),
    ]


def key(slots: list[PriceSlot] | None = None, **changes: object) -> tuple:
    arguments = {
        "revision": 0, "settings": settings(), "forecast_enabled": True,
        "opportunistic_enabled": False, "scheduled_loads": [], "soc": 50,
        "current_discharge_enabled": True, "override_action": "auto",
    }
    arguments.update(changes)
    return planning_key(prices() if slots is None else slots, **arguments)


def test_minute_refresh_ignores_clipped_load_and_normal_soc_drift() -> None:
    original = prices()
    minute_later = [replace(original[0], start=BASE + timedelta(minutes=1),
                            tariff_start=BASE, expected_load_wh=24),
                    *[replace(slot, expected_load_wh=99) for slot in original[1:]]]

    assert key(original, soc=50) == key(minute_later, soc=49.8)
    assert hash(key(original)) == hash(key(minute_later))


def test_raw_inputs_defer_learned_policy_and_forecast_changes_until_tariff() -> None:
    manual = {"capacity_kwh": 2., "round_trip_efficiency": 85.}
    inputs = {"market_inputs": (("sensor.prices", "DKK/kWh", 1.),),
              "configured_settings": manual}
    original = key(**inputs)
    derived = prices()
    derived[-1] = replace(derived[-1], price=2.8, uncertainty_dkk_per_kwh=.9)
    refined = key(derived, settings=replace(settings(), capacity_wh=1990,
                                          round_trip_efficiency=.86,
                                          minimum_profit_dkk_per_kwh=0.), **inputs)

    assert original == refined
    assert hash(original) == hash(refined)
    assert original != key(prices()[1:], **inputs)
    assert planning_change_reason(original, key(prices()[1:], **inputs)) == "tariff_boundary"
    manual["round_trip_efficiency"] = 86.
    assert original != key(**inputs)


def test_raw_market_basis_change_requires_a_new_plan() -> None:
    original = key(market_inputs=(("sensor.prices", "DKK/kWh", 1.),))
    revised = key(market_inputs=(("sensor.prices", "EUR/MWh", 1.),))
    assert original != revised
    assert planning_change_reason(original, revised) == "price_or_forecast_update"


def test_equal_price_tariff_boundary_still_requires_a_new_plan() -> None:
    assert key(prices()) != key(prices()[1:])


@pytest.mark.parametrize("change", [
    {"price": 2}, {"source": "known"},
    {"uncertainty_dkk_per_kwh": .5}, {"end": BASE + timedelta(hours=2)},
])
def test_future_price_and_forecast_revisions_require_a_new_plan(change) -> None:
    revised = prices()
    revised[-1] = replace(revised[-1], **change)
    assert key() != key(revised)


@pytest.mark.parametrize("change", [
    {"revision": 1}, {"settings": replace(settings(), reserve_soc=25)},
    {"settings": replace(settings(), round_trip_efficiency=.9)},
    {"forecast_enabled": False}, {"opportunistic_enabled": True},
    {"current_discharge_enabled": False}, {"override_action": "charge"},
    {"scheduled_loads": [{"start": BASE.isoformat(), "additional_w": 500}]},
])
def test_operator_and_physical_eligibility_changes_invalidate_plan(change) -> None:
    assert key() != key(**change)


@pytest.mark.parametrize("before,after", [(20.1, 20), (20, 19.9), (89.9, 90), (99.9, 100)])
def test_reserve_and_charge_limits_invalidate_plan(before, after) -> None:
    assert key(soc=before) != key(soc=after)


def test_schedule_dictionary_order_does_not_trigger_a_replan() -> None:
    assert key(scheduled_loads=[{"start": "a", "additional_w": 100}]) == key(
        scheduled_loads=[{"additional_w": 100, "start": "a"}],
    )


def test_equivalent_timezones_have_the_same_market_key() -> None:
    copenhagen = ZoneInfo("Europe/Copenhagen")
    local = [replace(slot, start=slot.start.astimezone(copenhagen),
                     end=slot.end.astimezone(copenhagen)) for slot in prices()]
    assert key() == key(local)


@pytest.mark.parametrize("minutes", [15, 60])
def test_next_boundary_uses_the_actual_tariff_cadence(minutes) -> None:
    slot = PriceSlot(BASE, BASE + timedelta(minutes=minutes), 1)
    assert earliest_future_boundary([slot], BASE + timedelta(minutes=1)) == slot.end
    assert earliest_future_boundary([slot], slot.end) is None


def test_future_start_wakes_up_a_missing_current_interval() -> None:
    slot = PriceSlot(BASE + timedelta(minutes=15), BASE + timedelta(hours=1), 1)
    assert earliest_future_boundary([slot], BASE) == slot.start


def test_boundary_selection_handles_dst_repeated_local_hour() -> None:
    copenhagen = ZoneInfo("Europe/Copenhagen")
    start = datetime(2026, 10, 25, 2, 45, tzinfo=copenhagen, fold=0)
    end = datetime(2026, 10, 25, 2, 0, tzinfo=copenhagen, fold=1)
    slot = PriceSlot(start, end, 1)
    assert earliest_future_boundary([slot], start) == end.astimezone(UTC)


def test_schedule_rejects_naive_times() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        earliest_future_boundary(prices(), BASE.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        key([replace(prices()[0], end=BASE.replace(tzinfo=None))])
