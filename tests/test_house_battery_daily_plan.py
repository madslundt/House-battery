"""Tests for re-anchoring replans to actual state and the persisted daily plan.

Covers the two coupled fixes:

* the optimizer starts every future plan (and its SOC curve) from the observed
  battery SOC instead of a projected/target value, and
* the daily plan is persisted, reconciled so the published past is immutable,
  and always shown as the complete local day (00:00 -> 24:00).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.dailyplan import (
    DailyPlan,
    local_day_bounds,
    merge_adjacent_blocks,
    reconcile_daily_plan,
)
from house_battery.models import Action, PlannerSettings, PlannedSlot, PriceSlot
from house_battery.planner import optimize

UTC = timezone.utc
# A fixed +02:00 zone keeps the day-boundary tests deterministic while still
# exercising timezone-aware (not UTC) calendar days.
PLUS2 = timezone(timedelta(hours=2))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def slot(
    start: datetime,
    end: datetime,
    action: Action,
    *,
    soc_start: float = 0.0,
    soc_end: float = 0.0,
) -> PlannedSlot:
    return PlannedSlot(
        start=start,
        end=end,
        action=action,
        price=1.0,
        expected_load_wh=100.0,
        grid_import_wh=0.0,
        battery_charge_wh=0.0,
        battery_discharge_wh=0.0,
        soc_start=soc_start,
        soc_end=soc_end,
        interval_cost_dkk=1.0,
        baseline_cost_dkk=2.0,
        reason="test",
    )


def assert_invariants(daily: DailyPlan) -> None:
    """The hard timeline invariants from requirement #7."""
    starts = [s.start for s in daily.slots]
    # sorted chronologically
    assert starts == sorted(starts)
    for previous, current in zip(daily.slots, daily.slots[1:]):
        # no zero-duration intervals and start < end
        assert previous.start < previous.end
        # no overlapping intervals; each point belongs to at most one interval
        assert previous.end <= current.start
        # no duplicate intervals
        assert (previous.start, previous.end, previous.action) != (
            current.start,
            current.end,
            current.action,
        )


def assert_within_day(daily: DailyPlan, day_start: datetime, day_end: datetime) -> None:
    for s in daily.slots:
        assert s.start >= day_start
        assert s.end <= day_end


# --------------------------------------------------------------------------- #
# #1 / #8 / #9 — Actual SOC wins over predicted SOC
# --------------------------------------------------------------------------- #

def _planner_settings() -> PlannerSettings:
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
        minimum_mode_minutes=30,
        maximum_transitions=4,
        energy_step_wh=25,
    )


def _price_slots(prices: list[float]) -> list[PriceSlot]:
    """Fill the whole local day (96 quarter-hours) so a mid-day ``now`` still
    has a future price horizon to optimize over."""
    base = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    count = max(len(prices) * 24, 96)
    return [
        PriceSlot(
            base + timedelta(minutes=15 * i),
            base + timedelta(minutes=15 * (i + 1)),
            prices[i % len(prices)],
            expected_load_wh=100 / 4,
        )
        for i in range(count)
    ]


def test_actual_soc_wins_over_predicted_soc() -> None:
    """A battery physically at 100% must plan from ~100%, not the 90% target."""
    now = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    plan = optimize(
        _price_slots([0.2] * 8 + [4.0] * 8),
        now=now,
        soc=100,  # actual observed SOC
        settings=_planner_settings(),
    )
    # The predicted/target-based start (~89%) is rejected; the future SOC curve
    # begins at the observed 100%.
    assert plan.slots[0].soc_start >= 99.0
    # And the battery is never allowed to charge above the configured target.
    assert all(slot_.soc_end <= 90.5 for slot_ in plan.slots)


def test_predicted_soc_is_not_treated_as_authoritative() -> None:
    """Even an earlier low prediction must not re-base the starting energy."""
    now = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    # A plan previously projected ~89% (soc capped to target); reality is 100%.
    plan_now = optimize(
        _price_slots([0.2] * 8 + [4.0] * 8),
        now=now,
        soc=100,
        settings=_planner_settings(),
    )
    plan_target_only = optimize(
        _price_slots([0.2] * 8 + [4.0] * 8),
        now=now,
        soc=90,  # the stale predicted value
        settings=_planner_settings(),
    )
    assert plan_now.slots[0].soc_start > plan_target_only.slots[0].soc_start


# --------------------------------------------------------------------------- #
# #3 / #5 — Recalculate only the future; truncate straddling intervals
# --------------------------------------------------------------------------- #

def test_replan_inside_an_existing_interval_truncates_cleanly() -> None:
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.BATTERY),
        ),
    )
    cutoff = datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    day_start, day_end = local_day_bounds(cutoff)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 15, 0, tzinfo=UTC), Action.GRID),
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, day_end=day_end
    )
    assert_invariants(result)
    assert [(s.start.time(), s.action) for s in result.slots] == [
        (datetime(2026, 9, 20, 12, 0).time(), Action.BATTERY),
        (datetime(2026, 9, 20, 13, 30).time(), Action.GRID),
    ]


def test_replan_exactly_on_a_boundary_has_no_duplicates_or_zero_duration() -> None:
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 13, 30, tzinfo=UTC), Action.BATTERY),
            slot(datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
                 datetime(2026, 9, 20, 15, 0, tzinfo=UTC), Action.GRID),
        ),
    )
    cutoff = datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    day_start, day_end = local_day_bounds(cutoff)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 15, 30, tzinfo=UTC), Action.CHARGE),
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, day_end=day_end
    )
    assert_invariants(result)
    # No duplicate 12:00 -> 13:30 interval and no zero-duration interval.
    assert [(s.start, s.end) for s in result.slots] == [
        (datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
         datetime(2026, 9, 20, 13, 30, tzinfo=UTC)),
        (datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
         datetime(2026, 9, 20, 15, 30, tzinfo=UTC)),
    ]


def test_history_before_the_cutoff_is_never_rewritten() -> None:
    """Requirement #11: the published past is immutable across replans."""
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 6, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 6, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 8, 0, tzinfo=UTC), Action.BATTERY),
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.CHARGE),
        ),
    )
    cutoff = datetime(2026, 9, 20, 13, 5, tzinfo=UTC)
    day_start, day_end = local_day_bounds(cutoff)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 15, 30, tzinfo=UTC), Action.GRID),
        slot(datetime(2026, 9, 20, 15, 30, tzinfo=UTC),
             datetime(2026, 9, 20, 17, 0, tzinfo=UTC), Action.CHARGE),
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, day_end=day_end
    )
    # The 00:00 -> 12:00 portion is byte-for-byte preserved.
    assert result.slots[0].action is Action.GRID
    assert result.slots[0].start == datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    assert result.slots[1].start == datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
    # The 12:00 -> 14:00 CHARGE interval is truncated to 13:05, not rewritten.
    assert result.slots[2].action is Action.CHARGE
    assert result.slots[2].end == cutoff


# --------------------------------------------------------------------------- #
# #6 — Merge identical adjacent intervals
# --------------------------------------------------------------------------- #

def test_same_action_on_both_sides_of_cutoff_merges() -> None:
    existing = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.BATTERY),
        ),
    )
    cutoff = datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    day_start, day_end = local_day_bounds(cutoff)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 15, 0, tzinfo=UTC), Action.BATTERY),
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, day_end=day_end
    )
    # The reconciled timeline keeps two touching BATTERY slots...
    assert_invariants(result)
    # ...and normalization merges them into a single logical interval.
    blocks = merge_adjacent_blocks(result.slots)
    assert len(blocks) == 1
    assert blocks[0].action is Action.BATTERY
    assert blocks[0].start == datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert blocks[0].end == datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
    # Economic detail is summed across the merged underlying slots.
    assert blocks[0].interval_cost_dkk == pytest.approx(2.0)


def test_merge_keeps_separate_when_actions_differ() -> None:
    slots = (
        slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 13, 0, tzinfo=UTC), Action.BATTERY),
        slot(datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.GRID),
    )
    blocks = merge_adjacent_blocks(slots)
    assert [b.action for b in blocks] == [Action.BATTERY, Action.GRID]


# --------------------------------------------------------------------------- #
# #7 — Consecutive replans keep the timeline clean
# --------------------------------------------------------------------------- #

def _day_bounds() -> tuple[datetime, datetime]:
    return local_day_bounds(datetime(2026, 9, 20, 0, 0, tzinfo=UTC))


def test_consecutive_replans_never_accumulate_gaps_or_overlaps() -> None:
    day_start, day_end = _day_bounds()
    # Seed a full-day plan.
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
    for minute in (5, 17, 46, 62):  # 13:05, 13:17, 13:46, 14:02
        cutoff = datetime(2026, 9, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)
        future = (
            slot(cutoff,
                 day_end,
                 Action.GRID if minute % 2 else Action.CHARGE),
        )
        daily = reconcile_daily_plan(
            daily, future, cutoff=cutoff, day_start=day_start, day_end=day_end
        )
        assert_invariants(daily)
        assert_within_day(daily, day_start, day_end)

    # Every point in the day is covered by exactly one interval (no gaps/overlaps).
    covered = 0.0
    previous_end = day_start
    for s in daily.slots:
        assert s.start == previous_end
        previous_end = s.end
    assert previous_end == day_end


# --------------------------------------------------------------------------- #
# #12 — Persistence + restart, and #Midnight
# --------------------------------------------------------------------------- #

def test_daily_plan_survives_serialization_and_restart() -> None:
    day_start, day_end = _day_bounds()
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
    assert reloaded.date == persisted.date
    assert [(s.start, s.end, s.action) for s in reloaded.slots] == [
        (s.start, s.end, s.action) for s in persisted.slots
    ]
    # Simulate a restart at 05:30 then a replan: the 00:00 timeline must remain.
    cutoff = datetime(2026, 9, 20, 5, 30, tzinfo=UTC)
    future = (
        slot(cutoff, datetime(2026, 9, 20, 9, 0, tzinfo=UTC), Action.GRID),
    )
    after_restart = reconcile_daily_plan(
        reloaded, future, cutoff=cutoff, day_start=day_start, day_end=day_end
    )
    assert_invariants(after_restart)
    assert after_restart.slots[0].start == datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    assert after_restart.slots[0].action is Action.GRID


def test_midnight_starts_a_fresh_timeline_and_does_not_merge_yesterday() -> None:
    yesterday = DailyPlan(
        date="2026-09-19",
        slots=(
            slot(datetime(2026, 9, 19, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 0, 0, tzinfo=UTC), Action.GRID),
        ),
    )
    today_start, today_end = local_day_bounds(
        datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
    )
    future = (
        slot(datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
             datetime(2026, 9, 20, 6, 0, tzinfo=UTC), Action.BATTERY),
    )
    result = reconcile_daily_plan(
        yesterday, future,
        cutoff=datetime(2026, 9, 20, 1, 0, tzinfo=UTC),
        day_start=today_start, day_end=today_end,
    )
    # Yesterday's plan is NOT merged into today.
    assert result.date == "2026-09-20"
    assert all(s.start >= today_start for s in result.slots)
    assert [(s.start.time(), s.action) for s in result.slots] == [
        (datetime(2026, 9, 20, 1, 0).time(), Action.BATTERY),
    ]


# --------------------------------------------------------------------------- #
# #10 — Timezone-aware bounds and arbitrary interval durations (no 15-min hack)
# --------------------------------------------------------------------------- #

def test_local_day_bounds_are_timezone_aware() -> None:
    now_plus2 = datetime(2026, 9, 20, 1, 30, tzinfo=PLUS2)
    start, end = local_day_bounds(now_plus2)
    assert start == datetime(2026, 9, 20, 0, 0, tzinfo=PLUS2)
    assert end == datetime(2026, 9, 21, 0, 0, tzinfo=PLUS2)
    # A UTC instant that is still the previous local day is handled correctly.
    now_utc = datetime(2026, 9, 20, 23, 30, tzinfo=UTC)
    start_utc, end_utc = local_day_bounds(now_utc)
    assert start_utc == datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


def test_variable_price_interval_durations_are_preserved() -> None:
    """No interval duration is hardcoded to 15 minutes."""
    day_start, day_end = local_day_bounds(datetime(2026, 9, 20, 0, 0, tzinfo=UTC))
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
    future = (
        slot(cutoff, datetime(2026, 9, 20, 13, 5, tzinfo=UTC), Action.GRID),  # 1h35m
    )
    result = reconcile_daily_plan(
        existing, future, cutoff=cutoff, day_start=day_start, day_end=day_end
    )
    assert_invariants(result)
    assert [(s.start.time(), s.end.time(), s.action) for s in result.slots] == [
        (datetime(2026, 9, 20, 10, 0).time(),
         datetime(2026, 9, 20, 11, 30).time(), Action.BATTERY),
        (datetime(2026, 9, 20, 11, 30).time(),
         datetime(2026, 9, 20, 13, 5).time(), Action.GRID),
    ]
    # The arbitrary durations survived (not snapped to 15-minute multiples).
    assert (result.slots[1].end - result.slots[1].start).total_seconds() == 95 * 60


# --------------------------------------------------------------------------- #
# #9 — SOC visualization: planned vs observed in the daily-plan view
# --------------------------------------------------------------------------- #

def test_daily_plan_view_exposes_planned_and_actual_soc() -> None:
    daily = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.BATTERY,
                 soc_start=89.0, soc_end=85.0),
        ),
    )
    view = daily.view_dict(actual_soc=100.0, terminal_price_dkk_per_kwh=3.21)
    assert view["actual_soc"] == 100.0
    assert view["blocks"][0]["actual_soc"] == 100.0
    # The published pre-replan projected SOC is left intact (deviation is visible).
    assert view["blocks"][0]["soc_start"] == 89.0
    assert view["terminal_price_dkk_per_kwh"] == 3.21


def test_daily_plan_view_always_covers_the_complete_day() -> None:
    """Future-only optimizer output must never be what the dashboard shows."""
    daily = DailyPlan(
        date="2026-09-20",
        slots=(
            slot(datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 6, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 6, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 8, 0, tzinfo=UTC), Action.BATTERY),
            slot(datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 12, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 14, 0, tzinfo=UTC), Action.CHARGE),
            slot(datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 17, 0, tzinfo=UTC), Action.GRID),
            slot(datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
                 datetime(2026, 9, 20, 22, 0, tzinfo=UTC), Action.BATTERY),
            slot(datetime(2026, 9, 20, 22, 0, tzinfo=UTC),
                 datetime(2026, 9, 21, 0, 0, tzinfo=UTC), Action.GRID),
        ),
    )
    view = daily.view_dict(actual_soc=100.0)
    # The example timeline from requirement #10: it starts at 00:00 and spans the
    # whole day, regardless of the current time.
    assert view["blocks"][0]["start"] == datetime(2026, 9, 20, 0, 0, tzinfo=UTC).isoformat()
    assert view["blocks"][-1]["end"] == datetime(2026, 9, 21, 0, 0, tzinfo=UTC).isoformat()
    assert len(view["blocks"]) == 7
