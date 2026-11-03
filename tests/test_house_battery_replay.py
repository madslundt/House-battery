"""Causal input archives and fair offline policy comparisons."""

import base64
import sys
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def test_retention_and_snapshot_bounds_are_explicit():
    history = ReplayHistory()
    for index in range(300):
        stamp = NOW + timedelta(seconds=index)
        record(history, stamp, slots=[replace(slots()[0], price=index / 100)])
    assert history.export()["snapshot_count"] == 256
    history.prune(NOW + timedelta(days=8))
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


def test_corrupt_persistence_never_creates_replay_evidence():
    assert ReplayHistory.from_dict({"schema_version": 999, "records": []}).as_dict()["records"] == []
    history = ReplayHistory.from_dict({"schema_version": 1, "records": [{"issued_at": "bad"}], "actuals": []})
    assert history.export()["snapshot_count"] == 0


def test_compressed_persistence_is_lossless_and_corruption_is_rejected(monkeypatch):
    history = ReplayHistory()
    record(history)
    actual(history)
    packed = history.as_storage_dict()
    assert packed["encoding"] == "zlib-base64"
    assert ReplayHistory.from_dict(packed).as_dict() == history.as_dict()
    assert ReplayHistory.from_dict({**packed, "payload": "bad"}).summary()["snapshot_count"] == 0
    monkeypatch.setattr("house_battery.replay.MAX_STATE_BYTES", 256)
    bomb = base64.b64encode(zlib.compress(b" " * 257)).decode()
    assert ReplayHistory.from_dict({**packed, "payload": bomb}).summary()["snapshot_count"] == 0


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


def test_snapshot_expires_inside_an_actual_interval_and_at_exact_boundary():
    history = ReplayHistory()
    record(history, slots=[PriceSlot(NOW, NOW + timedelta(hours=1), 2, 100)])
    history.record_actual({"start": NOW, "end": NOW + timedelta(hours=1),
                           "load_wh": 100, "covered_seconds": 3600,
                           "price": 2, "quality": "good"})
    result = evaluate_policies(history, settlement_price_dkk_per_kwh=1)
    assert result["evaluated_hours"] == .25
    assert result["skipped_hours"] == .75


def test_resolved_terminal_anchor_and_current_only_discharge_gate_are_archived(monkeypatch):
    history = ReplayHistory()
    issued = [PriceSlot(NOW, NOW + timedelta(minutes=5), 1, 10),
              PriceSlot(NOW + timedelta(minutes=5), NOW + timedelta(minutes=15), 1, 20)]
    assert record(history, slots=issued, terminal_price_floor_dkk_per_kwh=7,
                  current_discharge_enabled=False)
    restored = ReplayHistory.from_dict(history.as_storage_dict())
    assert restored.as_dict() == history.as_dict()
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
