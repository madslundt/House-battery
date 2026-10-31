"""Sizing uses covered completed days and qualified measurements, never guesses."""

import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PlannerSettings
from house_battery.sizing import SizingHistory, assess_sizing, sizing_prerequisites


def settings() -> PlannerSettings:
    return PlannerSettings(
        capacity_wh=1958, reserve_soc=10, target_soc=90,
        charge_power_w=1100, discharge_power_w=800,
        round_trip_efficiency=.8, degradation_cost_dkk_per_kwh=.35,
        minimum_profit_dkk_per_kwh=.5, switching_penalty_dkk=.05,
    )


def measurement(**changes) -> dict:
    return {"source": "independent-meter-fingerprint", "method": "matched_soc_grid_meter",
            "status": "ready", "planning_efficiency_pct": 80, "sample_count": 3, **changes}


def day_records(day: date, zone=UTC, coverage: float = 1) -> list[dict]:
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    start = start.replace(minute=0, second=0, microsecond=0)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    result = []
    while start < end:
        result.append({"start": start.isoformat(), "coverage_seconds": 3600 * coverage,
                       "load_wh": 250 * coverage, "price_seconds": 2 * 3600 * coverage})
        start += timedelta(hours=1)
    return result


def period(*, start=date(2026, 10, 1), days=28):
    rows = [row for index in range(days)
            for row in day_records(start + timedelta(days=index))]
    history = SizingHistory.from_dict({"source": "load-and-confirmed-price-scope", "hours": rows})
    now = datetime.combine(start + timedelta(days=days), datetime.min.time(), UTC)
    return history.completed_slots(now)


def assess(monkeypatch, *, current=28, half=14, expanded=30, summary=None, evidence=None, cost=3000):
    slots, recorded = period()

    def simulate(rows, policy):
        assert len(rows) == 28 * 24
        assert policy.reserve_soc == 10 and policy.target_soc == 90
        assert policy.round_trip_efficiency == .8
        assert policy.degradation_cost_dkk_per_kwh == .35
        net = half if policy.capacity_wh < 1958 else current if policy.capacity_wh == 1958 else expanded
        return {"net_savings_dkk": net, "bill_savings_dkk": net + 1,
                "wear_cost_dkk": .8, "switching_cost_dkk": .2}

    monkeypatch.setattr("house_battery.sizing.simulate_storage", simulate)
    return assess_sizing(
        slots, settings(), evidence=recorded if evidence is None else evidence,
        efficiency_summary=measurement() if summary is None else summary,
        expansion_capacity_kwh=1.958, expansion_cost_dkk=cost, payback_target_years=10,
    )


def test_expansion_must_clear_the_requested_after_wear_payback_target(monkeypatch):
    result = assess(monkeypatch, expanded=56)
    assert result["state"] == "bigger_battery_better"
    assert result["added_annualized_savings_dkk"] == pytest.approx(365)
    assert result["payback_years"] == pytest.approx(3000 / 365)
    assert result["capacity_cases"]["expanded"]["capacity_kwh"] == 3.916
    assert "hindsight" in result["method"]
    assert any("season" in item for item in result["assumptions"])


def test_positive_but_nonpaying_marginal_capacity_is_fine(monkeypatch):
    result = assess(monkeypatch)
    assert result["state"] == "size_is_fine"
    assert result["added_annualized_savings_dkk"] == pytest.approx(2 / 28 * 365)
    assert result["payback_years"] > 100


def test_free_expansion_with_positive_savings_has_zero_payback(monkeypatch):
    result = assess(monkeypatch, cost=0)
    assert result["state"] == "bigger_battery_better"
    assert result["payback_years"] == 0


def test_larger_capacity_retains_all_incumbent_economics_when_approximate_candidate_is_worse(monkeypatch):
    result = assess(monkeypatch, half=29, current=28, expanded=27)
    cases = result["capacity_cases"]
    for case in cases.values():
        assert case["net_savings_dkk"] == 29
        assert case["bill_savings_dkk"] == 30
        assert case["net_savings_dkk"] == pytest.approx(
            case["bill_savings_dkk"] - case["wear_cost_dkk"] - case["switching_cost_dkk"]
        )
        assert case["schedule_capacity_kwh"] == .979
        assert case["schedule_source"] == "half"
    assert cases["expanded"]["capacity_kwh"] == 3.916
    assert result["added_annualized_savings_dkk"] == 0
    assert result["payback_years"] is None


def test_half_capacity_must_retain_savings_and_lose_little_money(monkeypatch):
    assert assess(monkeypatch, half=27, expanded=29)["state"] == "battery_too_big"
    # The 95% test alone is insufficient when losing that 5% is expensive.
    assert assess(monkeypatch, current=1000, half=960, expanded=1001)["state"] == "size_is_fine"
    # Negligible savings cannot establish that capacity is oversized.
    assert assess(monkeypatch, current=1, half=1, expanded=1)["state"] == "size_is_fine"


@pytest.mark.parametrize("change", [
    {"status": "learning"}, {"status": "stale"}, {"source": ""},
    {"method": "battery_telemetry_diagnostic"}, {"sample_count": 2},
    {"planning_efficiency_pct": None}, {"planning_efficiency_pct": 72},
    {"planning_efficiency_pct": float("nan")},
])
def test_unqualified_efficiency_returns_learning_without_financial_claim(monkeypatch, change):
    result = assess(monkeypatch, summary=measurement(**change))
    assert result["state"] == "learning"
    assert result["added_annualized_savings_dkk"] is None
    assert result["payback_years"] is None
    assert result["capacity_cases"] == {}


@pytest.mark.parametrize("change", [
    {"day_count": 27}, {"weekend_days": 7}, {"coverage_pct": 94.9},
    {"span_days": 91}, {"stale": True},
])
def test_insufficient_or_biased_history_stays_learning(monkeypatch, change):
    _, evidence = period()
    assert assess(monkeypatch, evidence={**evidence, **change})["state"] == "learning"


def test_cheap_prerequisite_gate_uses_the_same_qualification():
    _, evidence = period()
    assert sizing_prerequisites(settings(), evidence=evidence, efficiency_summary=measurement()) is None
    assert sizing_prerequisites(settings(), evidence=evidence,
                                efficiency_summary=measurement(planning_efficiency_pct=72))


@pytest.mark.parametrize("change", [
    {"capacity_wh": float("nan")}, {"degradation_cost_dkk_per_kwh": float("inf")},
    {"switching_penalty_dkk": -.1},
])
def test_invalid_model_settings_cannot_support_a_capacity_recommendation(change):
    _, evidence = period()
    assert sizing_prerequisites(replace(settings(), **change), evidence=evidence,
                                efficiency_summary=measurement())


def test_observations_split_hour_and_confirmed_tariff_boundaries():
    history = SizingHistory()
    history.configure("connected-load:confirmed-price")
    first = datetime(2026, 10, 1, 0, 59, tzinfo=UTC)
    assert not history.observe(first, load_w=300, price=1)
    assert history.observe(first + timedelta(seconds=90), load_w=600, price=2,
                           price_start=first.replace(hour=1, minute=0))
    hours = history.as_dict()["hours"]
    assert [row["coverage_seconds"] for row in hours] == [60, 30]
    assert [row["load_wh"] for row in hours] == pytest.approx([5, 2.5])
    assert [row["price_seconds"] for row in hours] == [60, 60]


def test_gaps_invalid_samples_and_restarts_cannot_invent_coverage():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    history = SizingHistory()
    history.configure("scope")
    history.observe(start, load_w=300, price=2)
    history.observe(start + timedelta(seconds=60), load_w=300, price=2)
    history.observe(start + timedelta(seconds=300), load_w=300, price=2)
    restored = SizingHistory.from_dict(history.as_dict())
    restored.observe(start + timedelta(seconds=330), load_w=300, price=2)
    restored.observe(start + timedelta(seconds=390), load_w=300, price=2)
    restored.observe(start + timedelta(seconds=420), load_w=None, price=2)
    restored.observe(start + timedelta(seconds=450), load_w=300, price=2)
    assert sum(row["coverage_seconds"] for row in restored.as_dict()["hours"]) == 120
    slots, evidence = restored.completed_slots(start + timedelta(days=1))
    assert slots == [] and evidence["day_count"] == 0


def test_future_tariff_and_nonmonotonic_samples_break_continuity():
    history = SizingHistory()
    history.configure("scope")
    start = datetime(2026, 10, 1, tzinfo=UTC)
    history.observe(start, load_w=300, price=2)
    history.observe(start + timedelta(seconds=60), load_w=300, price=3,
                    price_start=start + timedelta(hours=1))
    history.observe(start + timedelta(seconds=90), load_w=300, price=2)
    history.observe(start + timedelta(seconds=80), load_w=300, price=2)
    history.observe(start + timedelta(seconds=120), load_w=300, price=2)
    assert history.as_dict()["hours"] == []


def test_out_of_order_observation_cannot_erase_already_recorded_energy():
    history = SizingHistory()
    history.configure("scope")
    start = datetime(2026, 10, 1, tzinfo=UTC)
    history.observe(start, load_w=300, price=2)
    history.observe(start + timedelta(seconds=60), load_w=300, price=2)
    saved = history.as_dict()
    history.observe(start + timedelta(seconds=30), load_w=300, price=2)
    assert history.as_dict() == saved
    history.observe(start + timedelta(seconds=90), load_w=300, price=2)
    assert history.as_dict() == saved
    restored = SizingHistory.from_dict(saved)
    restored.observe(start + timedelta(seconds=30), load_w=300, price=2)
    assert restored.as_dict() == saved


def test_changing_scope_discards_incompatible_history_and_continuity():
    history = SizingHistory()
    history.configure("old-load:prices")
    start = datetime(2026, 10, 1, tzinfo=UTC)
    history.observe(start, load_w=200, price=2)
    history.observe(start + timedelta(seconds=60), load_w=200, price=2)
    history.configure("different-load:prices")
    assert history.as_dict()["hours"] == []
    history.observe(start + timedelta(seconds=90), load_w=200, price=2)
    assert history.as_dict()["hours"] == []


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_completed_local_day_respects_dst(day, count):
    zone = ZoneInfo("Europe/Copenhagen")
    history = SizingHistory.from_dict({"source": "scope", "hours": day_records(day, zone)})
    now = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    slots, evidence = history.completed_slots(now)
    assert len(slots) == count
    assert sum(slot.hours for slot in slots) == count
    assert evidence["day_count"] == 1 and evidence["weekend_days"] == 1
    assert evidence["coverage_pct"] == 100
    assert all(slot.start.tzinfo == UTC for slot in slots)


def test_half_hour_timezone_has_28_qualified_local_days_with_clipped_hour_means():
    zone = ZoneInfo("Asia/Kolkata")
    start = date(2026, 10, 1)
    rows = [row for index in range(28)
            for row in day_records(start + timedelta(days=index), zone)]
    history = SizingHistory.from_dict({"source": "scope", "hours": rows})
    now = datetime(2026, 10, 29, 1, tzinfo=zone)
    slots, evidence = history.completed_slots(now)
    assert evidence["day_count"] == 28 and evidence["weekend_days"] == 8
    assert evidence["coverage_pct"] == 100
    assert sum(slot.hours for slot in slots) == 28 * 24
    assert sum(slot.expected_load_wh for slot in slots) == pytest.approx(28 * 24 * 250)
    assert slots[0].start == datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    assert slots[-1].end == datetime(2026, 10, 28, 18, 30, tzinfo=UTC)
    assert slots[0].hours == .5 and slots[-1].hours == .5
    assert all(left.end == right.start for left, right in pairwise(slots))
    assert sizing_prerequisites(settings(), evidence=evidence, efficiency_summary=measurement()) is None


@pytest.mark.parametrize(("day", "hours"), [
    (date(2026, 4, 5), 24.5), (date(2026, 10, 4), 23.5),
])
def test_half_hour_dst_change_preserves_local_day_energy_and_coverage(day, hours):
    zone = ZoneInfo("Australia/Lord_Howe")
    history = SizingHistory.from_dict({"source": "scope", "hours": day_records(day, zone)})
    now = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone) + timedelta(hours=1)
    slots, evidence = history.completed_slots(now)
    assert evidence["day_count"] == 1 and evidence["coverage_pct"] == 100
    assert sum(slot.hours for slot in slots) == hours
    assert sum(slot.expected_load_wh for slot in slots) == pytest.approx(hours * 250)
    assert all(slot.price == 2 for slot in slots)


@pytest.mark.parametrize("coverage", [.89, .90, .949])
def test_daily_coverage_cannot_be_faked_by_present_hour_buckets(coverage):
    history = SizingHistory.from_dict({
        "source": "scope", "hours": day_records(date(2026, 10, 1), coverage=coverage),
    })
    slots, evidence = history.completed_slots(datetime(2026, 10, 2, tzinfo=UTC))
    assert slots == [] and evidence["day_count"] == 0


def test_one_undercovered_hour_disqualifies_an_otherwise_complete_day():
    rows = day_records(date(2026, 10, 1))
    rows[0].update(coverage_seconds=3200, load_wh=200, price_seconds=6400)
    history = SizingHistory.from_dict({"source": "scope", "hours": rows})
    assert history.completed_slots(datetime(2026, 10, 2, tzinfo=UTC))[0] == []


def test_only_last_28_completed_valid_days_are_returned_and_old_history_is_stale():
    slots, evidence = period(days=35)
    assert len(slots) == 28 * 24
    assert evidence["available_valid_days"] == 35
    assert evidence["day_count"] == 28 and evidence["weekend_days"] == 8
    rows = [row for day in range(28) for row in day_records(date(2026, 8, 10) + timedelta(days=day))]
    history = SizingHistory.from_dict({"source": "scope", "hours": rows})
    _, old = history.completed_slots(datetime(2026, 10, 1, tzinfo=UTC))
    assert old["stale"]


def test_history_extends_recent_period_to_include_enough_weekends_without_dropping_weekdays():
    start = date(2026, 9, 24)
    missing = {date(2026, 9, 26), date(2026, 10, 18)}
    rows = [row for index in range(33)
            if (day := start + timedelta(days=index)) not in missing
            for row in day_records(day)]
    history = SizingHistory.from_dict({"source": "scope", "hours": rows})
    slots, evidence = history.completed_slots(datetime(2026, 10, 27, tzinfo=UTC))
    assert evidence["available_valid_days"] == 31
    assert evidence["day_count"] == 29
    assert evidence["weekend_days"] == 8
    assert len(slots) == 29 * 24
    assert slots[0].start.date() == date(2026, 9, 27)


def test_restore_rejects_malformed_duplicate_or_unscoped_buckets():
    row = day_records(date(2026, 10, 1))[0]
    history = SizingHistory.from_dict({"source": "scope", "hours": [
        row, row, None, {**row, "coverage_seconds": 4000},
        {**row, "start": "2026-10-01T01:30:00+00:00"},
        {**row, "load_wh": float("nan")},
    ]})
    assert len(history.as_dict()["hours"]) == 1
    assert SizingHistory.from_dict({"source": None, "hours": [row]}).as_dict()["hours"] == []
    assert SizingHistory.from_dict({"source": "scope", "hours": "bad"}).as_dict()["hours"] == []
    assert SizingHistory.from_dict(None).as_dict()["hours"] == []


def test_retention_and_future_samples_do_not_enter_completed_history():
    rows = day_records(date(2026, 1, 1)) + day_records(date(2026, 10, 31))
    history = SizingHistory.from_dict({"source": "scope", "hours": rows})
    slots, evidence = history.completed_slots(datetime(2026, 10, 31, 12, tzinfo=UTC))
    assert slots == [] and evidence["day_count"] == 0
    assert len(history.as_dict()["hours"]) <= 13
