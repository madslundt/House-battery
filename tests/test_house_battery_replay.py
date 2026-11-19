"""Causal input archives and fair offline policy comparisons."""

import base64
import json
import sys
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize
from house_battery.replay import ReplayHistory
from house_battery.replay_model import _Case, _execute, evaluate_policies

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
SETTINGS = PlannerSettings(200, 0, 100, 400, 200, 1, 0, .5, 0,
                          energy_step_wh=5, partial_charge_targets=())


def test_replay_accounts_for_charge_ceiling_stops_and_restarts():
    plant = replace(SETTINGS, capacity_wh=2000, charge_power_w=1000,
                    switching_penalty_dkk=.05)
    case = _Case(800, 800, plant)
    for target in (41, 42):
        _execute(case, Action.CHARGE, load_wh=50, hours=.25, price=2.4,
                 target_soc=target, plant=plant)
        assert case.action is Action.GRID
        # Repeated observations of an already capped Charge command must not
        # invent another physical restart while the battery remains idle.
        before = case.metrics["switching_cost_dkk"]
        _execute(case, Action.CHARGE, load_wh=50, hours=.25, price=2.4,
                 target_soc=target, plant=plant)
        assert case.metrics["switching_cost_dkk"] == before
    assert case.stored_wh == 840
    assert case.metrics["switching_cost_dkk"] == pytest.approx(.20)


def test_replay_full_rate_charging_continues_without_extra_stop_cost():
    plant = replace(SETTINGS, capacity_wh=2000, charge_power_w=1000,
                    switching_penalty_dkk=.05)
    case = _Case(800, 800, plant)
    for _ in range(3):
        _execute(case, Action.CHARGE, load_wh=50, hours=.25, price=2.4,
                 target_soc=90, plant=plant)
        assert case.action is Action.CHARGE
    assert case.stored_wh == 1400
    assert case.metrics["switching_cost_dkk"] == pytest.approx(.05)


def slots(prices=(1, 5)):
    return [PriceSlot(NOW + timedelta(minutes=15*i), NOW + timedelta(minutes=15*(i+1)),
                      price, 25) for i, price in enumerate(prices)]


def actual(history, offset=0, price=1):
    start = NOW + timedelta(minutes=offset)
    history.record_actual({"start": start.isoformat(),
                           "end": (start + timedelta(minutes=15)).isoformat(),
                           "load_wh": 25, "covered_seconds": 900,
                           "price": price, "quality": "good"})


def record(history, at=NOW, **overrides):
    return history.record(at, slots=overrides.pop("slots", slots()),
                          settings=overrides.pop("settings", SETTINGS),
                          soc=overrides.pop("soc", 0), current_action=Action.GRID,
                          scope="scope-v1", **overrides)


def test_archive_is_immutable_deduplicated_and_survives_roundtrip():
    history = ReplayHistory()
    data = slots()
    assert record(history, slots=data)
    assert not record(history, NOW + timedelta(seconds=1), slots=data)
    exported = history.as_dict()
    exported["records"][0]["slots"][0]["price"] = 100
    assert history.as_dict()["records"][0]["slots"][0]["price"] == 1
    actual(history)
    assert ReplayHistory.from_dict(history.as_dict()).as_dict() == history.as_dict()


def test_retention_and_snapshot_bounds_are_explicit(monkeypatch):
    monkeypatch.setattr("house_battery.replay.MAX_SNAPSHOTS", 20)
    history = ReplayHistory()
    for index in range(40):
        stamp = NOW + timedelta(seconds=index)
        record(history, stamp, slots=[replace(slots()[0], price=index / 100)])
    assert history.export()["snapshot_count"] == 20
    assert history.summary()["first_issued_at"] == (NOW + timedelta(seconds=20)).isoformat()
    assert ReplayHistory.from_dict(history.as_storage_dict()).as_dict() == history.as_dict()
    history.prune(NOW + timedelta(days=30))
    assert history.export()["snapshot_count"] == 0


def test_no_forecast_vintage_from_the_future_is_visible():
    history = ReplayHistory()
    record(history, NOW + timedelta(hours=1))
    actual(history)
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == 0
    assert result["skipped_hours"] == .25


def test_candidates_share_realized_plant_and_separate_inventory_from_cash():
    history = ReplayHistory()
    record(history)
    actual(history)
    record(history, NOW + timedelta(minutes=15), slots=slots())
    actual(history, 15, 5)
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == .5
    for name in ("current", "zero_profit"):
        case = result["policies"][name]
        assert case["energy_balance_error_wh"] == pytest.approx(0, abs=1e-9)
        assert case["cash_cost_dkk"] + case["wear_cost_dkk"] + case["switching_cost_dkk"] + case["inventory_settlement_dkk"] == pytest.approx(case["net_cost_dkk"])
        assert case["reserve_shortfall_wh"] == 0


def test_missing_actual_interval_breaks_storage_carryover():
    history = ReplayHistory()
    record(history)
    actual(history)
    record(history, NOW + timedelta(minutes=30), slots=[
        PriceSlot(NOW + timedelta(minutes=30), NOW + timedelta(minutes=45), 5, 25)])
    actual(history, 30, 5)
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["segments"] == 2
    assert result["actual_gap_hours"] == .25
    assert result["policies"]["current"]["discharge_kwh"] == 0


@pytest.fixture
def handoff_calls(monkeypatch):
    calls = []

    def plan(issued_slots, *, now, soc, settings, current_action, **_):
        calls.append((now, issued_slots[0].price, soc, current_action))
        if now == NOW:
            action = Action.CHARGE if settings.minimum_profit_dkk_per_kwh else Action.BATTERY
        else:
            action = Action.GRID
        return SimpleNamespace(slots=[
            SimpleNamespace(action=action, end=issued_slots[0].end, charge_target_soc=None),
        ])

    monkeypatch.setattr("house_battery.replay_model.optimize", plan)
    return calls


@pytest.mark.parametrize("delay", [timedelta(milliseconds=1), timedelta(seconds=1)])
def test_small_tariff_handoff_preserves_both_simulated_states(delay, handoff_calls):
    history = ReplayHistory()
    tariffs = slots()
    boundary = tariffs[1].start
    issued = boundary + delay
    assert record(history, slots=tariffs, soc=50)
    assert not history.close(issued, interrupted=False)
    assert record(history, issued, soc=22, slots=[
        replace(tariffs[1], start=issued, tariff_start=boundary),
    ])
    actual(history)
    actual(history, 15, 5)

    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)

    assert handoff_calls == [
        (NOW, 1, 50, Action.GRID),
        (NOW, 1, 50, Action.GRID),
        (issued, 5, 87.5, Action.CHARGE),
        (issued, 5, 37.5, Action.BATTERY),
    ]
    assert result["segments"] == 1
    assert result["skipped_hours"] == pytest.approx(delay.total_seconds() / 3600)
    assert result["evaluated_hours"] == pytest.approx(.5 - result["skipped_hours"])
    expected_cash_dkk = (25 + 5 * 25 * (1 - delay.total_seconds() / 900)) / 1000
    assert result["policies"]["no_battery"]["cash_cost_dkk"] == pytest.approx(expected_cash_dkk)


@pytest.mark.parametrize("interruption", [
    "late_issue", "missing_actual", "early_close", "boundary_close", "post_boundary_close", "instant_resume",
    "scope_change", "plant_change", "tariff_gap",
])
def test_replay_handoff_breaks_on_real_gaps_and_incompatible_inputs(interruption, handoff_calls):
    history = ReplayHistory()
    tariffs = slots()
    old, upcoming = tariffs
    measured_offset = 15
    if interruption == "missing_actual":
        interval = upcoming.end - upcoming.start
        upcoming = replace(upcoming, start=upcoming.start + interval, end=upcoming.end + interval)
        measured_offset = 30
    if interruption == "tariff_gap":
        gap = timedelta(milliseconds=100)
        upcoming = replace(upcoming, start=upcoming.start + gap, end=upcoming.end + gap)
    delays = {"late_issue": timedelta(seconds=1, microseconds=1), "instant_resume": timedelta(0)}
    delay = delays.get(interruption, timedelta(milliseconds=500))
    issued = upcoming.start + delay
    assert record(history, slots=tariffs, soc=50)
    close_offsets = {"early_close": -100, "boundary_close": 0, "post_boundary_close": 250, "instant_resume": 0}
    if interruption in close_offsets:
        assert history.close(old.end + timedelta(milliseconds=close_offsets[interruption]))
    assert history.record(
        issued, slots=[replace(upcoming, start=issued, tariff_start=upcoming.start)], soc=22,
        settings=replace(SETTINGS, capacity_wh=300) if interruption == "plant_change" else SETTINGS,
        scope="new-scope" if interruption == "scope_change" else "scope-v1",
    )
    actual(history)
    actual(history, measured_offset, 5)

    restored = ReplayHistory.from_dict(history.as_storage_dict())
    result = evaluate_policies(restored, settlement_price_dkk_per_kwh=1)

    assert result["segments"] == 2
    assert handoff_calls[-2:] == [(issued, 5, 22, Action.GRID)] * 2


def test_corrupt_persistence_never_creates_replay_evidence():
    assert ReplayHistory.from_dict({"schema_version": 999, "records": []}).as_dict()["records"] == []
    history = ReplayHistory.from_dict({"schema_version": 1, "records": [{"issued_at": "bad"}], "actuals": []})
    assert history.export()["snapshot_count"] == 0


def test_compressed_persistence_is_lossless_and_corruption_is_rejected(monkeypatch):
    history = ReplayHistory()
    record(history)
    actual(history)
    packed = history.as_storage_dict()
    assert packed["encoding"] == "zlib-base64-chunks"
    assert ReplayHistory.from_dict(packed).as_dict() == history.as_dict()
    broken = ReplayHistory.from_dict({**packed, "records": ["bad"]})
    assert broken.as_dict()["records"] == []
    assert broken.as_dict()["actuals"] == history.as_dict()["actuals"]
    monkeypatch.setattr("house_battery.replay.MAX_STATE_BYTES", 256)
    bomb = base64.b64encode(zlib.compress(b" " * 257)).decode()
    assert ReplayHistory.from_dict({**packed, "records": [bomb]}).summary()["snapshot_count"] == 0


def test_future_revision_cannot_rewrite_executed_prefix():
    history = ReplayHistory()
    record(history)
    actual(history)
    original = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    record(history, NOW + timedelta(minutes=15), slots=slots((99, .01)))
    revised = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert revised["policies"] == original["policies"]
    assert ReplayHistory.from_dict(history.as_storage_dict()).as_dict() == history.as_dict()


def test_scope_mismatch_is_skipped_instead_of_comparing_different_installations():
    history = ReplayHistory()
    record(history)
    history.record_actual({"start": NOW, "end": NOW + timedelta(minutes=15),
                           "load_wh": 25, "covered_seconds": 900, "price": 1,
                           "quality": "good", "scope": "another-installation"})
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == 0
    assert result["skipped_hours"] == .25


def test_initial_published_commitment_is_held_by_both_candidates():
    history = ReplayHistory()
    flat = slots((0, 0))
    committed = optimize(flat, now=NOW, soc=100, settings=SETTINGS).slots[0]
    assert committed.action is Action.GRID
    record(history, NOW + timedelta(minutes=1), soc=100, committed_slot=committed)
    actual(history)
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == pytest.approx(14 / 60)
    for name in ("current", "zero_profit"):
        assert result["policies"][name]["discharge_kwh"] == 0


def test_hourly_snapshot_covers_the_full_tariff_and_expires_at_its_end():
    history = ReplayHistory()
    hourly = [
        PriceSlot(NOW, NOW + timedelta(hours=1), 2, 100),
        PriceSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=2), 2, 100),
    ]
    record(history, slots=hourly)
    for minute in range(0, 75, 15):
        actual(history, minute, 2)
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == 1
    assert result["skipped_hours"] == .25
    assert result["segments"] == 1


def test_legacy_compressed_hourly_snapshot_keeps_its_original_expiry():
    history = ReplayHistory()
    record(history, slots=[PriceSlot(NOW, NOW + timedelta(hours=1), 2, 100)])
    for minute in range(0, 60, 15):
        actual(history, minute, 2)
    legacy = {**history.as_dict(), "schema_version": 1}
    del legacy["records"][0]["valid_until"]
    payload = base64.b64encode(zlib.compress(json.dumps(legacy).encode())).decode()
    restored = ReplayHistory.from_dict({"schema_version": 1, "encoding": "zlib-base64", "payload": payload})
    migrated = ReplayHistory.from_dict(restored.as_storage_dict())
    result = evaluate_policies(migrated, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == .25
    assert result["skipped_hours"] == .75


def test_interrupted_vintage_cannot_resume_after_a_newer_one_expires():
    history = ReplayHistory()
    hourly = [PriceSlot(NOW, NOW + timedelta(hours=1), 2, 100)]
    assert record(history, slots=hourly)
    assert record(history, NOW + timedelta(minutes=10), slots=hourly, soc=25)
    assert history.close(NOW + timedelta(minutes=22))
    assert not history.close(NOW + timedelta(minutes=23))
    for minute in range(0, 60, 15):
        actual(history, minute, 2)
    restored = ReplayHistory.from_dict(history.as_storage_dict())
    assert ReplayHistory.from_dict(history.as_dict()).as_dict() == restored.as_dict()
    result = evaluate_policies(restored, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == pytest.approx(22 / 60)
    assert result["skipped_hours"] == pytest.approx(38 / 60)


def test_compact_archive_retains_and_evaluates_four_weeks():
    history = ReplayHistory()
    quarters = 28 * 96
    for index in range(quarters):
        minute = index * 15
        start = NOW + timedelta(minutes=minute)
        record(history, start, slots=[PriceSlot(start, start + timedelta(minutes=15), 1, 25)])
        actual(history, minute)
    runtime_export = history.export(compact=True)
    history = ReplayHistory.from_dict(runtime_export)
    assert history.summary()["snapshot_count"] == history.summary()["actual_count"] == quarters
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == 28 * 24
    assert result["skipped_hours"] == 0
    assert result["segments"] == 1
    result = evaluate_policies(
        history, settlement_price_dkk_per_kwh=1, since=NOW + timedelta(days=14),
    )
    assert result["evaluated_hours"] == result["measured_coverage_hours"] == 14 * 24
    assert result["skipped_hours"] == 0


def test_save_recompresses_only_changed_archive_pages(monkeypatch):
    history = ReplayHistory()
    for index in range(17):
        stamp = NOW + timedelta(minutes=15 * index)
        boundary = stamp + timedelta(minutes=15)
        record(history, stamp, slots=[
            PriceSlot(stamp, boundary, 1, 25 + index),
            PriceSlot(boundary, boundary + timedelta(minutes=15), 1, 35 + index),
            PriceSlot(boundary + timedelta(minutes=15), boundary + timedelta(minutes=30),
                      1.25, 30, "forecast", .25, boundary + timedelta(minutes=15)),
        ])
    actual(history)
    original = history.as_storage_dict()
    compress = zlib.compress
    compressed = []

    def track(data, **kwargs):
        compressed.append(data)
        return compress(data, **kwargs)

    monkeypatch.setattr(zlib, "compress", track)
    assert history.as_storage_dict() == original
    assert compressed == []

    actual(history, 15)
    changed_actuals = history.as_storage_dict()
    assert changed_actuals["records"] == original["records"]
    assert len(compressed) == 1

    assert history.close(NOW + timedelta(minutes=16 * 15 + 1))
    changed_record = history.as_storage_dict()
    assert changed_record["records"][:-1] == original["records"][:-1]
    assert changed_record["actuals"] == changed_actuals["actuals"]
    assert len(compressed) == 2
    assert ReplayHistory.from_dict(changed_record).as_dict() == history.as_dict()


@pytest.mark.parametrize("invalid", [{"price": "unknown"}, {"covered_seconds": 0}])
def test_invalid_compact_actual_pages_are_excluded(invalid):
    history = ReplayHistory()
    actual(history)
    packed = history.as_storage_dict()
    broken = {**history.as_dict()["actuals"][0], **invalid}
    packed["actuals"] = [base64.b64encode(zlib.compress(json.dumps([broken]).encode())).decode()]
    assert ReplayHistory.from_dict(packed).summary()["actual_count"] == 0


def test_resolved_terminal_anchor_and_current_only_discharge_gate_are_archived(monkeypatch):
    history = ReplayHistory()
    issued = [PriceSlot(NOW, NOW + timedelta(minutes=5), 1, 10),
              PriceSlot(NOW + timedelta(minutes=5), NOW + timedelta(minutes=15), 1, 20)]
    assert record(history, slots=issued, terminal_price_floor_dkk_per_kwh=7,
                  current_discharge_enabled=False)
    restored = ReplayHistory.from_dict(history.as_storage_dict())
    assert restored.as_dict() == history.as_dict()
    assert record(restored, NOW + timedelta(minutes=5), slots=issued,
                  terminal_price_floor_dkk_per_kwh=7)
    actual(restored)
    forwarded = []

    def spy(*args, **kwargs):
        forwarded.append((kwargs["terminal_price_floor_dkk_per_kwh"],
                          kwargs.pop("current_discharge_enabled")))
        return optimize(*args, **kwargs)

    monkeypatch.setattr("house_battery.replay_model.optimize", spy)
    evaluate_policies(restored, settlement_price_dkk_per_kwh=1)
    assert forwarded[:2] == [(7, False), (7, False)]
    assert forwarded[2:] == [(7, True), (7, True)]


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True])
def test_invalid_terminal_anchor_is_not_archived(value):
    assert not record(ReplayHistory(), terminal_price_floor_dkk_per_kwh=value)
