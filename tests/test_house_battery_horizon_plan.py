"""Horizon-aware published-plan regression tests.

These close the gap described by the planning requirements: the persisted
published timeline must span the *whole* currently known planning horizon
(``local 00:00 today -> horizon_end``) with an immutable past before the replan
cutoff and a continuously replaceable future re-anchored to the observed SOC.

The suite deliberately covers the full coordinator -> optimizer -> reconciler
chain (so an API mismatch such as ``horizon_end`` vs ``day_end`` can never
remain latent), plus every invariant the requirements enumerate: full horizon
past midnight, immutable history, future replacement, straddling-interval
clipping with correct proration, repeated replans without duplicates/overlaps,
actual-SOC re-anchoring, ``actual_soc_at``, restart persistence, mid-day cold
start, midnight rollover, DST/local timezones, variable interval durations,
horizon-dependent load forecasting and tomorrow's plan staying visible.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from homeassistant.core import HomeAssistant

from house_battery.const import CONF_LOAD_POWER, CONF_PRICE_ENTITIES
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.dailyplan import (
    DailyPlan,
    local_day_bounds,
    merge_adjacent_blocks,
    reconcile_daily_plan,
)
from house_battery.learning import LoadLearner
from house_battery.models import Action, PlannerSettings, PlannedSlot, PriceSlot
from house_battery.planner import optimize
from house_battery.runtime import RuntimeState

UTC = timezone.utc
PLUS2 = timezone(timedelta(hours=2))
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def slot(
    start: datetime,
    end: datetime,
    action: Action,
    *,
    price: float = 1.0,
    expected_load_wh: float = 100.0,
    grid_import_wh: float = 0.0,
    battery_charge_wh: float = 0.0,
    battery_discharge_wh: float = 0.0,
    soc_start: float = 0.0,
    soc_end: float = 0.0,
    interval_cost_dkk: float = 1.0,
    baseline_cost_dkk: float = 2.0,
    reason: str = "test",
    price_source: str = "known",
) -> PlannedSlot:
    return PlannedSlot(
        start=start,
        end=end,
        action=action,
        price=price,
        expected_load_wh=expected_load_wh,
        grid_import_wh=grid_import_wh,
        battery_charge_wh=battery_charge_wh,
        battery_discharge_wh=battery_discharge_wh,
        soc_start=soc_start,
        soc_end=soc_end,
        interval_cost_dkk=interval_cost_dkk,
        baseline_cost_dkk=baseline_cost_dkk,
        reason=reason,
        price_source=price_source,
    )


def assert_invariants(daily: DailyPlan) -> None:
    """The hard timeline invariants from the requirements."""
    starts = [s.start for s in daily.slots]
    assert starts == sorted(starts), "slots must be sorted chronologically"
    for previous, current in zip(daily.slots, daily.slots[1:]):
        assert previous.start < previous.end, "zero/negative duration interval"
        assert previous.end <= current.start, "overlapping intervals"
        assert (previous.start, previous.end) != (
            current.start,
            current.end,
        ), "duplicate interval"


def assert_contiguous(daily: DailyPlan, start: datetime, end: datetime) -> None:
    """Every instant in ``[start, end)`` belongs to exactly one interval."""
    cursor = start
    for s in daily.slots:
        assert s.start == cursor, f"gap or overlap at {cursor}"
        cursor = s.end
    assert cursor == end, "timeline must reach the horizon end exactly"


def planner_settings() -> PlannerSettings:
    return PlannerSettings(
        capacity_wh=1958,
        reserve_soc=20,
        target_soc=90,
        charge_power_w=1200,
        discharge_power_w=800,
        round_trip_efficiency=0.85,
        degradation_cost_dkk_per_kwh=0.35,
        minimum_profit_dkk_per_kwh=0.75,
        switching_penalty_dkk=0.05,
        energy_step_wh=25,
    )


def hourly_prices(
    start: datetime, count: int, prices: list[float]
) -> list[dict[str, str]]:
    return [
        {
            "start": (start + timedelta(hours=i)).isoformat(),
            "end": (start + timedelta(hours=i + 1)).isoformat(),
            "price": str(prices[i % len(prices)]),
        }
        for i in range(count)
    ]


class _Entry:
    entry_id = "horizon-test"
    title = "Horizon test"

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.options: dict[str, object] = {}

    def async_on_unload(self, callback: object) -> None:
        del callback


def _coordinator_with_prices(
    hass: HomeAssistant, start: datetime, prices: list[float], hours: int
) -> Fbp1200Coordinator:
    entry = _Entry(
        {CONF_PRICE_ENTITIES: ["sensor.price"], CONF_LOAD_POWER: "sensor.load"}
    )
    coordinator = Fbp1200Coordinator(hass, entry)
    hass.states.async_set(
        "sensor.price",
        "1.0",
        {"prices": hourly_prices(start, hours, prices)},
    )
    hass.states.async_set("sensor.load", "110")
    return coordinator


def _coordinator_horizon_chain(
    now_local: datetime, prices: list[float], *, days: int = 3
) -> tuple[DailyPlan, datetime, datetime, datetime]:
    """Run the live coordinator price pipeline -> optimizer -> reconciler.

    A persisted morning plan (midnight -> now) represents the history the
    coordinator keeps across replans, so the published timeline begins at local
    midnight.
    """
    now = now_local.astimezone(UTC)
    start = now - timedelta(hours=2)

    async def _build() -> object:
        hass = HomeAssistant("/tmp")
        coordinator = _coordinator_with_prices(hass, start, prices, days * 24 + 4)
        future_slots = coordinator._price_slots(now)
        plan = optimize(
            future_slots,
            now=now,
            soc=65.0,
            settings=planner_settings(),
        )
        return coordinator, plan

    coordinator, plan = asyncio.run(_build())
    day_start, _ = local_day_bounds(now_local)
    coordinator.runtime.daily_plan = DailyPlan(
        date=now_local.date().isoformat(),
        slots=(slot(day_start, now, Action.GRID),),
    )
    horizon_end = plan.slots[-1].end
    daily = reconcile_daily_plan(
        coordinator.runtime.daily_plan,
        plan.slots,
        cutoff=now,
        day_start=day_start,
        horizon_end=horizon_end,
    )
    assert_invariants(daily)
    return daily, day_start, horizon_end, now


# --------------------------------------------------------------------------- #
# P0 — real coordinator -> optimizer -> published-plan reconciliation
# --------------------------------------------------------------------------- #


def test_coordinator_optimizer_reconcile_spans_the_full_horizon() -> None:
    """Requirement P0: coordinator -> optimizer -> reconciler spans
    local 00:00 today -> horizon_end (which extends past midnight)."""
    now_local = datetime(2026, 9, 20, 10, 0, tzinfo=PLUS2)  # 08:00 UTC
    daily, day_start, horizon_end, now = _coordinator_horizon_chain(
        now_local, [0.2] * 20 + [5.0] * 20 + [1.0] * 40
    )
    # The published timeline begins at local midnight today (immutable history).
    assert daily.slots[0].start == day_start
    assert daily.slots[0].start.utcoffset() == timedelta(hours=2)
    assert daily.slots[0].action is Action.GRID
    # ...and reaches the known-price horizon end (two days ahead, past midnight).
    assert daily.slots[-1].end == horizon_end
    assert_contiguous(daily, daily.slots[0].start, horizon_end)
    # At least one published block is dated tomorrow (prices are known).
    tomorrow = (day_start + timedelta(days=1)).date()
    assert any(s.end.date() >= tomorrow for s in daily.slots)
    # The past (midnight -> now) is exactly the immutable history we set.
    assert daily.slots[0].end == now


def test_coordinator_horizon_end_used_not_day_end() -> None:
    """Regression: the reconciler is driven by ``horizon_end``, never ``day_end``.

    A today-only interpretation would stop the timeline at local midnight; the
    full horizon must run well past it.
    """
    now_local = datetime(2026, 9, 20, 10, 0, tzinfo=PLUS2)
    daily, day_start, horizon_end, _now = _coordinator_horizon_chain(
        now_local, [1.0] * 80
    )
    day_end = day_start + timedelta(days=1)
    assert horizon_end > day_end
    assert daily.slots[-1].end == horizon_end
    assert daily.slots[-1].end > day_end


# --------------------------------------------------------------------------- #
# Full known horizon extending beyond midnight
# --------------------------------------------------------------------------- #


def test_full_horizon_extends_past_midnight() -> None:
    """A morning plan plus a multi-day future optimizer output spans the whole
    horizon, past local midnight."""
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    horizon_end = day_start + timedelta(days=2, hours=5)
    cutoff = day_start + timedelta(hours=8)
    existing = DailyPlan(
        date="2026-09-20",
        slots=(slot(day_start, cutoff, Action.GRID),),
    )
    daily = reconcile_daily_plan(
        existing,
        (slot(cutoff, horizon_end, Action.BATTERY),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=horizon_end,
    )
    assert_invariants(daily)
    assert_contiguous(daily, day_start, horizon_end)
    last = daily.slots[-1]
    assert last.end.date() == datetime(2026, 9, 22).date()
    assert last.start.date() == datetime(2026, 9, 20).date()
    # The immutable morning history is preserved byte-for-byte.
    assert daily.slots[0].action is Action.GRID
    assert daily.slots[0].end == cutoff


# --------------------------------------------------------------------------- #
# Immutable history before ``now``
# --------------------------------------------------------------------------- #


def test_immutable_history_before_cutoff_is_preserved() -> None:
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    cutoff = datetime(2026, 9, 20, 7, 30, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 4, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 4, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 7, 30, tzinfo=UTC), Action.BATTERY),
        ),
    )
    result = reconcile_daily_plan(
        existing,
        (slot(cutoff, day_end, Action.CHARGE),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=day_end,
    )
    # The morning is byte-for-byte immutable.
    assert [(s.start, s.end, s.action) for s in result.slots[:2]] == [
        (datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
         datetime(2026, 9, 20, 4, 0, tzinfo=UTC), Action.GRID),
        (datetime(2026, 9, 20, 4, 0, tzinfo=UTC),
         datetime(2026, 9, 20, 7, 30, tzinfo=UTC), Action.BATTERY),
    ]
    # The future is fully replaced from ``now``.
    assert result.slots[2].start == cutoff
    assert result.slots[2].action is Action.CHARGE


# --------------------------------------------------------------------------- #
# Replacement of all future intervals from ``now``
# --------------------------------------------------------------------------- #


def test_all_future_intervals_replaced_from_now() -> None:
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    cutoff = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 8, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 10, 0, tzinfo=UTC), Action.CHARGE),
        ),
    )
    # The old 08:00->10:00 CHARGE straddles the cutoff, so it is prated to
    # 08:00->08:30 and the fresh future takes over from ``now``.
    future = (
        slot(cutoff, datetime(2026, 9, 20, 9, 0, tzinfo=UTC), Action.BATTERY),
        slot(datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 12, 0, tzinfo=UTC), Action.GRID),
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, horizon_end=day_end
    )
    assert_invariants(result)
    # The immutable morning GRID survives.
    assert result.slots[0].action is Action.GRID
    assert result.slots[0].end == datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    # The 08:00->10:00 CHARGE straddles the cutoff, so it is prated to 08:00->08:30.
    assert result.slots[1].action is Action.CHARGE
    assert result.slots[1].start == datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    assert result.slots[1].end == cutoff
    # The future replaces the remainder of the straddling CHARGE block.  Its
    # re-optimised tail stops at the persisted interval end (10:00); the tail is
    # the optimiser's own price intervals, clipped so nothing crosses 10:00.
    assert [(s.start, s.end, s.action) for s in result.slots[2:]] == [
        (cutoff, datetime(2026, 9, 20, 9, 0, tzinfo=UTC), Action.BATTERY),
        (datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
         datetime(2026, 9, 20, 10, 0, tzinfo=UTC), Action.GRID),
        (datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
         datetime(2026, 9, 20, 12, 0, tzinfo=UTC), Action.GRID),
    ]


# --------------------------------------------------------------------------- #
# Interval crossing the replan cutoff + correct proration/interpolation
# --------------------------------------------------------------------------- #


def test_interval_crossing_cutoff_is_prorated_and_interpolated() -> None:
    """A truncated interval keeps its start but prorates every extensive
    quantity and interpolates its end SOC to the shortened duration."""
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(
                datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
                Action.BATTERY,
                expected_load_wh=200.0,
                grid_import_wh=50.0,
                battery_discharge_wh=100.0,
                soc_start=90.0,
                soc_end=80.0,
                interval_cost_dkk=4.0,
                baseline_cost_dkk=6.0,
                reason="Battery clears the economic margin",
            ),
        ),
    )
    cutoff = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)  # exactly half the interval
    day_start, day_end = local_day_bounds(cutoff)
    result = reconcile_daily_plan(
        existing,
        (slot(cutoff, datetime(2026, 9, 20, 15, 0, tzinfo=UTC), Action.GRID),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=day_end,
    )
    assert_invariants(result)
    head = result.slots[0]
    assert head.start == datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert head.end == cutoff
    # Fraction retained = 1h / 2h = 0.5, applied to every extensive quantity.
    assert head.expected_load_wh == pytest.approx(100.0)
    assert head.grid_import_wh == pytest.approx(25.0)
    assert head.battery_discharge_wh == pytest.approx(50.0)
    assert head.interval_cost_dkk == pytest.approx(2.0)
    assert head.baseline_cost_dkk == pytest.approx(3.0)
    # SOC interpolated linearly: 90 + (80 - 90) * 0.5 = 85, start unchanged.
    assert head.soc_start == pytest.approx(90.0)
    assert head.soc_end == pytest.approx(85.0)
    # Action and justification are unchanged by the truncation.
    assert head.action is Action.BATTERY
    assert head.reason == "Battery clears the economic margin"


def test_charge_interval_soc_interpolates_upward() -> None:
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(
                datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
                Action.CHARGE,
                soc_start=40.0,
                soc_end=60.0,
            ),
        ),
    )
    cutoff = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    day_start, day_end = local_day_bounds(cutoff)
    result = reconcile_daily_plan(
        existing,
        (slot(cutoff, day_end, Action.GRID),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=day_end,
    )
    head = result.slots[0]
    assert head.soc_start == pytest.approx(40.0)
    assert head.soc_end == pytest.approx(50.0)  # 40 + (60-40)*0.5


def test_straddling_interval_proration_preserves_contiguity() -> None:
    """The truncated head and the fresh future join exactly at the cutoff."""
    existing = DailyPlan(
        date="2026-09-20",
        slots=(slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                    datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.BATTERY),),
    )
    cutoff = datetime(2026, 9, 20, 13, 20, tzinfo=UTC)
    future_end = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
    result = reconcile_daily_plan(
        existing,
        (slot(cutoff, future_end, Action.GRID),),
        cutoff=cutoff,
        day_start=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
        horizon_end=future_end,
    )
    assert_invariants(result)
    head, future = result.slots[0], result.slots[1]
    assert head.end == future.start == cutoff
    assert_contiguous(result, result.slots[0].start, future_end)


# --------------------------------------------------------------------------- #
# Repeated replans without duplicates/overlaps
# --------------------------------------------------------------------------- #


def test_repeated_replans_never_accumulate_duplicates_or_overlaps() -> None:
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    daily = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 12, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.BATTERY),
            slot(datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
                 datetime(2026, 9, 21, 0, 0, tzinfo=UTC), Action.GRID),
        ),
    )
    for minute in (5, 17, 46, 62, 90, 150):
        cutoff = datetime(2026, 9, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)
        future = (
            slot(cutoff, day_end, Action.GRID if minute % 2 else Action.CHARGE),
        )
        daily = reconcile_daily_plan(
            daily, future, cutoff=cutoff, day_start=day_start, horizon_end=day_end
        )
        assert_invariants(daily)
    # Fully contiguous with no gaps, overlaps or duplicates across all replans.
    assert_contiguous(daily, day_start, day_end)
    # Exactly one interval per disjoint span: no accumulation.
    covered = sum((s.end - s.start).total_seconds() for s in daily.slots)
    assert covered == (day_end - day_start).total_seconds()


# --------------------------------------------------------------------------- #
# Actual SOC re-anchoring
# --------------------------------------------------------------------------- #


def test_future_soc_curve_reanchors_to_actual_soc() -> None:
    """The optimizer plans from the observed SOC, so the future blocks start
    there (within a rounding point), independent of any earlier projection."""
    now = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    actual_soc = 73.5
    plan = optimize(
        [
            PriceSlot(
                now + timedelta(hours=i),
                now + timedelta(hours=i + 1),
                0.2 if i < 4 else 4.0,
                expected_load_wh=125.0,
            )
            for i in range(24)
        ],
        now=now,
        soc=actual_soc,
        settings=planner_settings(),
    )
    assert plan.slots
    # Re-anchored to the observed SOC: within a rounding point of 73.5,
    # clearly not a stale projection such as 88.
    assert abs(plan.slots[0].soc_start - actual_soc) < 1.5
    assert plan.slots[0].soc_end >= 72.0


def test_reconcile_keeps_future_soc_at_actual_and_history_projected() -> None:
    """The future re-anchors to the observed SOC; the immutable projected
    history keeps its (different) projected SOC."""
    now = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    actual_soc = 73.5
    plan = optimize(
        [
            PriceSlot(
                now + timedelta(hours=i),
                now + timedelta(hours=i + 1),
                0.2,
                expected_load_wh=125.0,
            )
            for i in range(6)
        ],
        now=now,
        soc=actual_soc,
        settings=planner_settings(),
    )
    day_start, day_end = local_day_bounds(now)
    # A projected historical head whose projected SOC does not match the
    # observed SOC (88% was the forecast, 73.5% is what we actually measure).
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(day_start, now, Action.BATTERY, soc_start=92.0, soc_end=88.0),
        ),
    )
    result = reconcile_daily_plan(
        existing,
        plan.slots,
        cutoff=now,
        day_start=day_start,
        horizon_end=plan.slots[-1].end,
    )
    future = result.slots[1]
    # Future re-anchors at the observed SOC.
    assert abs(future.soc_start - actual_soc) < 1.5
    # The projected history head keeps its projected SOC (immutable past).
    assert result.slots[0].soc_end == pytest.approx(88.0)


# --------------------------------------------------------------------------- #
# actual_soc_at
# --------------------------------------------------------------------------- #


def test_view_exposes_actual_soc_and_actual_soc_at_without_touching_blocks() -> None:
    daily = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 6, 0, tzinfo=UTC), Action.GRID,
                 soc_start=100.0, soc_end=100.0),
        ),
    )
    view = daily.view_dict(
        actual_soc=73.5, actual_soc_at="2026-09-20T13:05:00+02:00"
    )
    assert view["actual_soc"] == 73.5
    assert view["actual_soc_at"] == "2026-09-20T13:05:00+02:00"
    # The current observation is NOT bound to the midnight block.
    assert "actual_soc" not in view["blocks"][0]
    # The published projected SOC curve is untouched.  A grid block that never
    # moves the battery collapses its identical start/end SOC into one ``soc``
    # field, so the projected reading is still visible, just not duplicated.
    assert view["blocks"][0]["soc"] == 100.0
    assert "soc_start" not in view["blocks"][0]
    assert "soc_end" not in view["blocks"][0]


def test_view_without_actual_soc_reports_none() -> None:
    daily = DailyPlan(date="2026-09-20")
    view = daily.view_dict()
    assert view["actual_soc"] is None
    assert view["actual_soc_at"] is None


def test_view_reports_history_available_from_midnight_on_full_day() -> None:
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    daily = DailyPlan(
        date="2026-09-20",
        slots=(slot(day_start, day_start + timedelta(hours=6), Action.GRID),),
    )
    view = daily.view_dict()
    assert view["history_available_from"] == day_start.isoformat()


def test_view_reports_history_available_from_cutoff_on_cold_start() -> None:
    cutoff = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    daily = DailyPlan(
        date="2026-09-20",
        slots=(slot(cutoff, cutoff + timedelta(hours=6), Action.BATTERY),),
    )
    view = daily.view_dict()
    assert view["history_available_from"] == cutoff.isoformat()


# --------------------------------------------------------------------------- #
# Restart persistence
# --------------------------------------------------------------------------- #


def test_history_survives_restart_then_replan() -> None:
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    persisted = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 6, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 6, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 8, 0, tzinfo=UTC), Action.BATTERY),
        ),
        created_at=datetime(2026, 9, 20, 5, 0, tzinfo=UTC),
    )
    reloaded = DailyPlan.from_dict(persisted.as_dict())
    assert [(s.start, s.end) for s in reloaded.slots] == [
        (s.start, s.end) for s in persisted.slots
    ]
    # Restart at 05:30 then replan: the 00:00 history must survive.
    cutoff = datetime(2026, 9, 20, 5, 30, tzinfo=UTC)
    after_restart = reconcile_daily_plan(
        reloaded,
        (slot(cutoff, day_end, Action.GRID),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=day_end,
    )
    assert_invariants(after_restart)
    assert after_restart.slots[0].start == day_start
    assert after_restart.slots[0].action is Action.GRID
    assert_contiguous(after_restart, day_start, day_end)


def test_restart_with_no_plan_survives_then_replan_from_now() -> None:
    """A restarted device with no persisted plan replans from ``now``; the
    timeline begins at ``now`` (no invented midnight history)."""
    cutoff = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    horizon_end = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    empty = DailyPlan(date="")
    restarted = RuntimeState(daily_plan=empty).daily_plan
    result = reconcile_daily_plan(
        restarted,
        (slot(cutoff, horizon_end, Action.BATTERY),),
        cutoff=cutoff,
        day_start=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
        horizon_end=horizon_end,
    )
    assert_invariants(result)
    assert result.slots[0].start == cutoff
    assert_contiguous(result, result.slots[0].start, horizon_end)


# --------------------------------------------------------------------------- #
# Cold start during the day
# --------------------------------------------------------------------------- #


def test_cold_start_does_not_invent_history_before_now() -> None:
    """First plan created mid-day: no 00:00 history, history_available_from
    marks the replan cutoff."""
    day_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    horizon_end = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    cutoff = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    daily = reconcile_daily_plan(
        None,
        (slot(cutoff, horizon_end, Action.BATTERY),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=horizon_end,
    )
    assert_invariants(daily)
    # Nothing is published before the cutoff (no invented midnight history).
    assert daily.slots[0].start == cutoff
    assert all(s.start >= cutoff for s in daily.slots)
    view = daily.view_dict()
    assert view["history_available_from"] == cutoff.isoformat()
    assert view["blocks"][0]["start"] == cutoff.isoformat()


# --------------------------------------------------------------------------- #
# Midnight rollover
# --------------------------------------------------------------------------- #


def test_midnight_rollover_starts_fresh_timeline_without_yesterday() -> None:
    yesterday = DailyPlan(
        date="2026-09-19",
        slots=(
            slot(datetime(2026, 9, 19, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 0, 0, tzinfo=UTC), Action.GRID),
        ),
    )
    today_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    cutoff = datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 6, 0, tzinfo=UTC), Action.BATTERY),
    )
    result = reconcile_daily_plan(
        yesterday,
        future,
        cutoff=cutoff,
        day_start=today_start,
        horizon_end=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )
    assert result.date == "2026-09-20"
    # Yesterday's timeline is not merged in; nothing predates local midnight.
    assert all(s.start >= today_start for s in result.slots)
    assert [(s.start.time(), s.action) for s in result.slots] == [
        (datetime(2026, 9, 20, 1, 0).time(), Action.BATTERY),
    ]


def test_midnight_future_may_continue_into_tomorrow() -> None:
    """Today's timeline begins at 00:00 yet future intervals continue past
    midnight when they belong to the known horizon."""
    today_start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    horizon_end = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    cutoff = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
    existing = DailyPlan(
        date="2026-09-20",
        slots=(slot(today_start, cutoff, Action.GRID),),
    )
    result = reconcile_daily_plan(
        existing,
        (slot(cutoff, horizon_end, Action.BATTERY),),
        cutoff=cutoff,
        day_start=today_start,
        horizon_end=horizon_end,
    )
    assert result.slots[0].start == today_start
    assert result.slots[-1].end == horizon_end
    assert result.slots[-1].end.date() == datetime(2026, 9, 21).date()


# --------------------------------------------------------------------------- #
# DST / local timezone handling
# --------------------------------------------------------------------------- #


def test_local_day_bounds_are_dst_safe_in_copenhagen() -> None:
    """Midnight is resolved to the correct pre/post-DST offset."""
    spring_midnight = datetime(2026, 3, 29, 0, 0, tzinfo=COPENHAGEN)
    start, end = local_day_bounds(spring_midnight)
    # 00:00 on the spring-forward day is still CET (+01:00); the next wall-clock
    # midnight is CEST (+02:00) after the jump.  The two bounds therefore carry
    # *different* offsets, which is exactly the DST-safety guarantee.
    assert start.utcoffset() == timedelta(hours=1)
    assert end.utcoffset() == timedelta(hours=2)


def test_reconcile_across_the_spring_forward_keeps_instant_contiguity_and_offsets() -> (
    None
):
    """Across a DST transition every slot keeps its wall-time offset and the
    timeline stays contiguous in instant space."""
    tz = COPENHAGEN
    # ``now`` is just before the 02:00 -> 03:00 spring-forward jump, so the
    # immutable morning history carries the pre-transition CET offset (+01:00)
    # while future intervals past 03:00 carry CEST (+02:00).
    now_local = datetime(2026, 3, 29, 1, 0, tzinfo=tz)  # 01:00 CET (+01:00)
    day_start, _ = local_day_bounds(now_local)
    assert day_start.utcoffset() == timedelta(hours=1)  # 00:00 still CET
    horizon_end = day_start + timedelta(hours=26)  # reaches into CEST
    cutoff = now_local.astimezone(UTC)
    boundary = now_local + timedelta(hours=3)  # 04:00 CEST (+02:00)
    existing = DailyPlan(
        date="2026-03-29",
        slots=(slot(day_start, now_local, Action.BATTERY),),
    )
    future = (
        slot(cutoff, boundary, Action.GRID),
        slot(boundary, horizon_end, Action.CHARGE),
    )
    result = reconcile_daily_plan(
        existing,
        future,
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=horizon_end,
    )
    assert_invariants(result)
    # The earliest slot keeps the pre-transition CET (+01:00) offset.
    assert result.slots[0].start.utcoffset() == timedelta(hours=1)
    # Future slots past the 03:00 CEST boundary carry the +02:00 offset.
    assert any(s.start.utcoffset() == timedelta(hours=2) for s in result.slots)
    # Instant-contiguous across the whole 26-hour span.
    cursor = result.slots[0].start.astimezone(UTC)
    for s in result.slots:
        assert s.start.astimezone(UTC) == cursor
        cursor = s.end.astimezone(UTC)
    assert cursor == horizon_end.astimezone(UTC)


def test_reconcile_across_fall_back_transition_is_contiguous() -> None:
    tz = COPENHAGEN
    now_local = datetime(2026, 10, 25, 12, 0, tzinfo=tz)  # CET after fall-back
    day_start, _ = local_day_bounds(now_local)
    horizon_end = day_start + timedelta(hours=30)
    cutoff = now_local.astimezone(UTC)
    result = reconcile_daily_plan(
        None,
        (slot(cutoff, horizon_end, Action.CHARGE),),
        cutoff=cutoff,
        day_start=day_start,
        horizon_end=horizon_end,
    )
    assert_invariants(result)
    # Fall-back day is 25 h; the timeline spans it contiguously in instants.
    cursor = result.slots[0].start.astimezone(UTC)
    for s in result.slots:
        assert s.start.astimezone(UTC) == cursor
        cursor = s.end.astimezone(UTC)
    assert cursor == horizon_end.astimezone(UTC)
    # A slot after 03:00 carries the reverted +01:00 offset.
    assert any(s.start.utcoffset() == timedelta(hours=1) for s in result.slots)


# --------------------------------------------------------------------------- #
# Variable price interval durations
# --------------------------------------------------------------------------- #


def test_variable_interval_durations_are_preserved_after_clipping() -> None:
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 11, 45, tzinfo=UTC), Action.BATTERY),  # 1h45m
            slot(datetime(2026, 9, 20, 11, 45, tzinfo=UTC),
                 datetime(2026, 9, 20, 12, 15, tzinfo=UTC), Action.GRID),  # 30m
        ),
    )
    cutoff = datetime(2026, 9, 20, 11, 30, tzinfo=UTC)
    day_start, day_end = local_day_bounds(cutoff)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 13, 5, tzinfo=UTC), Action.GRID),  # 1h35m
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, horizon_end=day_end
    )
    assert_invariants(result)
    # The clipped 1h45m BATTERY head keeps its arbitrary duration.
    head = result.slots[0]
    assert (head.end - head.start).total_seconds() == 90 * 60
    # The straddling BATTERY interval is re-anchored at the cutoff; its
    # re-optimised grid tail (15 min) and the following future interval (80 min)
    # follow.  The arbitrary durations survive -- they are not snapped to
    # 15-minute multiples (80 is not a multiple).
    assert (result.slots[1].end - result.slots[1].start).total_seconds() == 15 * 60
    assert (result.slots[2].end - result.slots[2].start).total_seconds() == 80 * 60
    assert head.action is Action.BATTERY


# --------------------------------------------------------------------------- #
# Horizon-dependent load forecasting (P1)
# --------------------------------------------------------------------------- #


def _well_sampled_learner() -> LoadLearner:
    learner = LoadLearner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    for week in range(14):  # plenty of history -> evidence_factor == 1
        learner.observe(when - timedelta(weeks=week), 500)
    learner.recent_w.clear()
    learner.recent_w.extend([100] * 8)  # recent usage is low
    return learner


def test_near_future_trusts_recent_usage() -> None:
    learner = _well_sampled_learner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    prediction = learner.predict_w(when, 100, now=when)  # distance 0
    # Very close to now: recent usage (100 W) dominates the 500 W profile.
    assert prediction == pytest.approx(140.0, abs=1.0)
    assert prediction < 200


def test_longer_future_trusts_historical_profile() -> None:
    learner = _well_sampled_learner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    prediction = learner.predict_w(when, 100, now=when - timedelta(hours=24))
    # 24 h ahead: the historical profile (500 W) dominates.
    assert prediction == pytest.approx(380.0, abs=2.0)
    assert prediction > 300


def test_prediction_increases_monotonically_with_horizon() -> None:
    """The same bucket must not use a fixed recent/history blend: nearer
    predictions trust recent usage, farther ones trust history."""
    learner = _well_sampled_learner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    near = learner.predict_w(when, 100, now=when - timedelta(minutes=30))
    medium = learner.predict_w(when, 100, now=when - timedelta(hours=6))
    long_term = learner.predict_w(when, 100, now=when - timedelta(hours=18))
    assert near < medium < long_term
    # Near stays close to recent usage; long-term approaches the historical mean.
    assert near < 175
    assert long_term > 340


def test_no_now_argument_treats_prediction_as_near() -> None:
    """Backward-compatible call (used by the online learning update) always
    weights recent usage heavily regardless of the bucket."""
    learner = _well_sampled_learner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    prediction = learner.predict_w(when)  # no `now`
    assert prediction == pytest.approx(140.0, abs=1.0)


# --------------------------------------------------------------------------- #
# Tomorrow's plan stays visible when tomorrow's prices are available
# --------------------------------------------------------------------------- #


def test_tomorrow_plan_visible_when_tomorrow_prices_available() -> None:
    """Known prices covering tomorrow must produce a published timeline that
    reaches into tomorrow."""
    now_local = datetime(2026, 9, 20, 9, 0, tzinfo=PLUS2)  # 07:00 UTC
    now = now_local.astimezone(UTC)
    start = now - timedelta(hours=1)

    async def _build() -> object:
        hass = HomeAssistant("/tmp")
        coordinator = _coordinator_with_prices(hass, start, [1.0] * 100, 50)
        future_slots = coordinator._price_slots(now)
        plan = optimize(future_slots, now=now, soc=50.0, settings=planner_settings())
        day_start, _ = local_day_bounds(now_local)
        coordinator.runtime.daily_plan = DailyPlan(
            date=now_local.date().isoformat(),
            slots=(slot(day_start, now, Action.GRID),),
        )
        horizon_end = plan.slots[-1].end
        daily = reconcile_daily_plan(
            coordinator.runtime.daily_plan,
            plan.slots,
            cutoff=now,
            day_start=day_start,
            horizon_end=horizon_end,
        )
        return daily, day_start, horizon_end, now

    daily, day_start, horizon_end, _now = asyncio.run(_build())
    tomorrow = (day_start + timedelta(days=1)).date()
    # Blocks are published for tomorrow, not silently dropped.
    assert any(s.start.date() >= tomorrow for s in daily.slots)
    assert daily.slots[-1].end > day_start + timedelta(days=1)
    # The morning before now remains as immutable history from midnight.
    assert daily.slots[0].start == day_start
    assert_invariants(daily)


# --------------------------------------------------------------------------- #
# Merging hygiene (P2): do not merge incompatible metadata
# --------------------------------------------------------------------------- #


def test_merge_combines_same_action_different_price() -> None:
    """Two adjacent CHARGE blocks at different prices DO merge into one block.

    Merging is action-only by design: the block view is the overview, so a run
    of price quarters collapses regardless of price.  The shown price/reason
    come from the first sub-interval; the per-quarter economic detail is kept in
    the persisted slots and the summed energy/cost stay faithful.
    """
    slots = (
        slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 1, 0, tzinfo=UTC), Action.CHARGE, price=0.2,
             reason="cheap"),
        slot(datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 2, 0, tzinfo=UTC), Action.CHARGE, price=3.0,
             reason="expensive"),
    )
    blocks = merge_adjacent_blocks(slots)
    assert len(blocks) == 1
    assert blocks[0].action is Action.CHARGE
    # The overview shows the first sub-interval's price/reason; the merged span
    # still aggregates the underlying economics faithfully.
    assert blocks[0].price == pytest.approx(0.2)


def test_merge_combines_same_action_differing_reason() -> None:
    slots = (
        slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 1, 0, tzinfo=UTC), Action.CHARGE,
             reason="first justification"),
        slot(datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 2, 0, tzinfo=UTC), Action.CHARGE,
             reason="second justification"),
    )
    blocks = merge_adjacent_blocks(slots)
    assert len(blocks) == 1
    assert blocks[0].action is Action.CHARGE


def test_merge_keeps_compatible_intervals_and_sums_aggregates() -> None:
    slots = (
        slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 1, 0, tzinfo=UTC), Action.BATTERY,
             expected_load_wh=100, battery_discharge_wh=200,
             interval_cost_dkk=1.0, soc_start=90, soc_end=88),
        slot(datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 2, 0, tzinfo=UTC), Action.BATTERY,
             expected_load_wh=100, battery_discharge_wh=200,
             interval_cost_dkk=1.0, soc_start=88, soc_end=86),
    )
    blocks = merge_adjacent_blocks(slots)
    assert len(blocks) == 1
    merged = blocks[0]
    assert merged.action is Action.BATTERY
    assert merged.price == pytest.approx(1.0)
    # Aggregates sum; the merged SOC spans the contiguous curve endpoints.
    assert merged.battery_discharge_wh == pytest.approx(400)
    assert merged.expected_load_wh == pytest.approx(200)
    assert merged.interval_cost_dkk == pytest.approx(2.0)
    assert merged.soc_start == pytest.approx(90)
    assert merged.soc_end == pytest.approx(86)


def test_merge_collapses_the_user_grid_grid_battery_example() -> None:
    """The reported example: three 15-minute quarters compress to two blocks.

    ``00:00-00:15 grid`` + ``00:15-00:30 grid`` + ``00:30-00:45 battery`` ->
    ``00:00-00:30 grid`` + ``00:30-00:45 battery``.
    """
    slots = (
        slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 0, 15, tzinfo=UTC), Action.GRID,
             price=0.5, reason="grid-a"),
        slot(datetime(2026, 9, 20, 0, 15, tzinfo=UTC),
             datetime(2026, 9, 20, 0, 30, tzinfo=UTC), Action.GRID,
             price=0.9, reason="grid-b"),
        slot(datetime(2026, 9, 20, 0, 30, tzinfo=UTC),
             datetime(2026, 9, 20, 0, 45, tzinfo=UTC), Action.BATTERY,
             price=0.9, reason="discharge"),
    )
    blocks = merge_adjacent_blocks(slots)
    assert len(blocks) == 2
    assert blocks[0].action is Action.GRID
    assert blocks[0].start == datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    assert blocks[0].end == datetime(2026, 9, 20, 0, 30, tzinfo=UTC)
    assert blocks[1].action is Action.BATTERY
    assert blocks[1].start == datetime(2026, 9, 20, 0, 30, tzinfo=UTC)
    assert blocks[1].end == datetime(2026, 9, 20, 0, 45, tzinfo=UTC)
