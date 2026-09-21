"""Coordinator gate tests for discretionary extra battery storage."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.coordinator import _strict_extra_storage_rejection
from house_battery.models import Action, Plan, PlannedSlot, PriceSlot


NOW = datetime(2026, 9, 21, 10, tzinfo=UTC)


def price_slot(
    index: int, price: float, *, source: str = "known"
) -> PriceSlot:
    start = NOW + timedelta(minutes=15 * index)
    return PriceSlot(start, start + timedelta(minutes=15), price, source=source)


def plan(*, cost: float, charge_slot: PriceSlot | None = None) -> Plan:
    slots: tuple[PlannedSlot, ...] = ()
    if charge_slot:
        slots = (
            PlannedSlot(
                start=charge_slot.start,
                end=charge_slot.end,
                action=Action.CHARGE,
                price=charge_slot.price,
                expected_load_wh=0,
                grid_import_wh=250,
                battery_charge_wh=230,
                battery_discharge_wh=0,
                soc_start=89,
                soc_end=100,
                interval_cost_dkk=cost,
                baseline_cost_dkk=0,
                reason="test",
            ),
        )
    return Plan(NOW, slots, cost, 0, -cost, 0, 0, "test")


def test_extra_plan_is_accepted_only_for_a_shortest_known_price_opportunity() -> None:
    cheapest = price_slot(0, 0.1)
    slots = [cheapest, price_slot(1, 2.0)]

    assert (
        _strict_extra_storage_rejection(
            normal_plan=plan(cost=2.0),
            extra_plan=plan(cost=1.0, charge_slot=cheapest),
            slots=slots,
            now=NOW,
            normal_target_soc=90,
            cheap_window_minutes=30,
        )
        is None
    )


def test_extra_plan_requires_a_real_incremental_cost_saving() -> None:
    cheapest = price_slot(0, 0.1)

    assert "no incremental expected-cost saving" in _strict_extra_storage_rejection(
        normal_plan=plan(cost=1.0),
        extra_plan=plan(cost=1.0, charge_slot=cheapest),
        slots=[cheapest],
        now=NOW,
        normal_target_soc=90,
        cheap_window_minutes=30,
    )


def test_extra_plan_rejects_a_forecast_price_even_when_it_is_cheapest() -> None:
    forecast = price_slot(0, 0.01, source="forecast")
    known = price_slot(1, 0.1)

    assert "not in a known-price interval" in _strict_extra_storage_rejection(
        normal_plan=plan(cost=2.0),
        extra_plan=plan(cost=1.0, charge_slot=forecast),
        slots=[forecast, known],
        now=NOW,
        normal_target_soc=90,
        cheap_window_minutes=30,
    )


def test_extra_plan_requires_the_cheapest_conservative_known_price() -> None:
    cheap = price_slot(0, 0.1)
    merely_low = price_slot(1, 0.2)

    assert "not at the cheapest known charge price" in _strict_extra_storage_rejection(
        normal_plan=plan(cost=2.0),
        extra_plan=plan(cost=1.0, charge_slot=merely_low),
        slots=[cheap, merely_low],
        now=NOW,
        normal_target_soc=90,
        cheap_window_minutes=30,
    )


def test_extra_plan_rejects_a_known_price_that_stays_cheapest_too_long() -> None:
    cheapest = price_slot(0, 0.1)
    equally_cheap = price_slot(1, 0.1)
    still_equally_cheap = price_slot(2, 0.1)

    assert "above the 30-minute extra-storage limit" in _strict_extra_storage_rejection(
        normal_plan=plan(cost=2.0),
        extra_plan=plan(cost=1.0, charge_slot=cheapest),
        slots=[cheapest, equally_cheap, still_equally_cheap],
        now=NOW,
        normal_target_soc=90,
        cheap_window_minutes=30,
    )
