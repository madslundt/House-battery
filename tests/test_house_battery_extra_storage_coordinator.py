"""Tests for the simplified coordinator (extra-storage removed).

Extra-storage target and spread knobs were removed (2025-09) because they
created wasteful discharge→recharge cycles.  The coordinator now uses a
single plan with target_soc as the hard charge ceiling.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

import homeassistant.util.dt as dt_util
import house_battery.coordinator as coordinator_module
from house_battery.const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_SOC,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.models import Action, Plan, PlannedSlot
from house_battery.runtime import RuntimeState


NOW = datetime(2026, 9, 21, 10, tzinfo=UTC)


def _make_plan(slots) -> Plan:
    return Plan(
        created_at=NOW,
        slots=slots,
        expected_cost_dkk=sum(s.interval_cost_dkk for s in slots),
        baseline_cost_dkk=sum(s.baseline_cost_dkk for s in slots),
        expected_savings_dkk=0,
        battery_throughput_kwh=0,
        terminal_price_dkk_per_kwh=1.0,
        reason="test",
    )


def test_plan_today_dict_filters_out_tomorrow() -> None:
    """today_dict should only include slots for today, not tomorrow."""
    today_slot = PlannedSlot(
        start=NOW,
        end=NOW + timedelta(hours=1),
        action=Action.GRID,
        price=1.0,
        expected_load_wh=100,
        grid_import_wh=100,
        battery_charge_wh=0,
        battery_discharge_wh=0,
        soc_start=50,
        soc_end=50,
        interval_cost_dkk=0.1,
        baseline_cost_dkk=0.1,
        reason="today",
    )
    tomorrow_slot = PlannedSlot(
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
        action=Action.GRID,
        price=1.0,
        expected_load_wh=100,
        grid_import_wh=100,
        battery_charge_wh=0,
        battery_discharge_wh=0,
        soc_start=50,
        soc_end=50,
        interval_cost_dkk=0.1,
        baseline_cost_dkk=0.1,
        reason="tomorrow",
    )
    full_plan = _make_plan((today_slot, tomorrow_slot))
    today_data = full_plan.today_dict(NOW)
    assert len(today_data["slots"]) == 1
    assert today_data["slots"][0]["reason"] == "today"


def test_plan_today_dict_uses_local_day_not_utc_day() -> None:
    """A non-UTC local day must keep the whole remaining local day.

    Regression: at 00:19 local (UTC+2) the UTC calendar day still runs until
    local 02:00, so a UTC-based boundary truncated the Operation plan sensor
    to the next two hours — an all-"grid" block — and hid the rest of the
    day's charge/battery decisions even though the optimizer had planned
    them.
    """
    local = timezone(timedelta(hours=2))
    local_now = datetime(2026, 9, 23, 0, 19, tzinfo=local)  # 22:19 UTC
    day_start = datetime(2026, 9, 23, tzinfo=local)
    slots = []
    for i in range(96):
        start = day_start + timedelta(minutes=15 * i)
        if start < local_now:
            continue
        slots.append(
            PlannedSlot(
                start=start,
                end=start + timedelta(minutes=15),
                action=Action.BATTERY if (i // 16) % 2 else Action.GRID,
                price=2.0,
                expected_load_wh=25,
                grid_import_wh=25,
                battery_charge_wh=0,
                battery_discharge_wh=0,
                soc_start=50,
                soc_end=50,
                interval_cost_dkk=0.05,
                baseline_cost_dkk=0.05,
                reason="test",
            )
        )
    full_plan = _make_plan(tuple(slots))
    today_data = full_plan.today_dict(local_now)
    # A UTC-based boundary (23:59:59 UTC == 01:59:59 local) would keep only
    # the 6 slots starting before local 02:00.  The local day keeps
    # everything until local 23:59:59 — 94 slots.
    assert len(today_data["slots"]) == 94


class _Entry:
    """Minimal ConfigEntry contract needed by DataUpdateCoordinator."""

    entry_id = "plan-display-test"
    title = "FBP1200"

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.options: dict[str, object] = {}

    def async_on_unload(self, callback: object) -> None:
        del callback


class _StaticStore:
    def __init__(self, state: RuntimeState) -> None:
        self.state = state

    async def load(self) -> RuntimeState:
        return self.state

    async def save(self, state: RuntimeState) -> None:
        self.state = state


def test_plan_view_spans_whole_local_day_at_utc_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 00:19 local (UTC+2) the coordinator plan view must not stop at UTC midnight.

    Regression: the Operation plan sensor showed a single 2 h 'grid' block
    because the coordinator passed UTC now into ``Plan.today_dict``.  With the
    local day the same feed yields the full evening/night plus morning and
    evening battery windows decided from known prices, SOC, and load.
    """
    local = ZoneInfo("Europe/Copenhagen")
    fixed_now = datetime(2026, 9, 23, 0, 19, 38, 305717, tzinfo=local)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - stdlib signature
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    # One local day: cheap baseline, 06:30-08:00 and 18:00-20:00 peaks.
    prices: list[dict[str, object]] = []
    for index in range(96):
        start = datetime(2026, 9, 23, tzinfo=local) + timedelta(minutes=15 * index)
        if 26 <= index <= 28:
            price = 4.0
        elif 72 <= index <= 79:
            price = 3.5
        else:
            price = 1.5
        prices.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=15)).isoformat(),
                "price": price,
            }
        )

    async def scenario() -> dict[str, object]:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        hass.config.time_zone = "Europe/Copenhagen"
        coordinator = Fbp1200Coordinator(
            hass,
            _Entry(
                {
                    CONF_SOC: "sensor.soc",
                    CONF_LOAD_POWER: "sensor.load",
                    CONF_GRID_IMPORT_POWER: "sensor.grid_import",
                    CONF_GRID_AVAILABLE: "binary_sensor.grid",
                    CONF_OPERATING_MODE: "select.mode",
                    CONF_BATTERY_CHARGE_POWER: "sensor.charge_power",
                    CONF_BATTERY_DISCHARGE_POWER: "sensor.discharge_power",
                    CONF_PRICE_ENTITIES: ["sensor.price"],
                }
            ),
        )
        coordinator.store = _StaticStore(RuntimeState())
        hass.states.async_set("sensor.price", "1.5", {"prices": prices})
        hass.states.async_set("sensor.soc", "89.377")
        hass.states.async_set("sensor.load", "150")
        hass.states.async_set("sensor.grid_import", "50")
        hass.states.async_set("binary_sensor.grid", "on")
        hass.states.async_set("select.mode", "Idle")
        hass.states.async_set("sensor.charge_power", "0")
        hass.states.async_set("sensor.discharge_power", "0")

        return await coordinator._async_update_data()

    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", local)
    monkeypatch.setattr(coordinator_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        coordinator_module,
        "get_health_problems",
        lambda *args, **kwargs: [],
    )
    data = asyncio.run(scenario())

    assert data["healthy"] is True
    assert data["system_state"] == "SHADOW"
    plan = data["plan"]
    slots = plan["slots"]
    # The full local day (95 slots incl. the truncated first one), not the
    # 7-slot UTC-day remnant.  (FbpPlanSensor exposes this length as its
    # `horizon_slots` attribute.)
    assert len(slots) == 95
    assert slots[0]["start"] == "2026-09-22T22:19:38.305717+00:00"
    assert slots[-1]["start"] == "2026-09-23T23:45:00+02:00"
    # The planner still decides from known prices, SOC, and load: cheap night
    # runs on grid, both price peaks run on the battery.
    assert slots[0]["action"] == "grid"
    assert slots[27]["action"] == "battery"  # 07:00 local, 4.0 DKK peak
    assert any(
        slot["action"] == "battery"
        for slot in slots
        if slot["start"] >= "2026-09-23T18:00:00+02:00"
    )
    assert {slot["action"] for slot in slots} == {"grid", "battery"}


def test_plan_today_dict_returns_all_when_all_today() -> None:
    """When all slots are for today, today_dict returns all of them."""
    slots = tuple(
        PlannedSlot(
            start=NOW + timedelta(hours=i),
            end=NOW + timedelta(hours=i + 1),
            action=Action.GRID,
            price=1.0,
            expected_load_wh=100,
            grid_import_wh=100,
            battery_charge_wh=0,
            battery_discharge_wh=0,
            soc_start=50,
            soc_end=50,
            interval_cost_dkk=0.1,
            baseline_cost_dkk=0.1,
            reason=f"hour_{i}",
        )
        for i in range(10)
    )
    full_plan = _make_plan(slots)
    today_data = full_plan.today_dict(NOW)
    assert len(today_data["slots"]) == 10
