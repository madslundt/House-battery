"""Matched-SOC learning uses real grid input and delivered load, never the plug."""

import json
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.efficiency import MatchedSocEfficiency


class Trace:
    def __init__(self, model: MatchedSocEfficiency | None = None) -> None:
        self.model = model or MatchedSocEfficiency()
        self.model.configure("phase-l1:backup-load:soc:v1")
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def sample(
        self, activity: str, soc: float, *, seconds: float = 60,
        charge_w: float = 0, unrelated_w: float = 50, load_w: float = 250,
        healthy: bool = True, reported_charge_w: float | None = None,
    ) -> None:
        self.now += timedelta(seconds=seconds)
        grid_w = unrelated_w + (load_w + 14 if activity != "discharging" else 0)
        self.model.observe(
            self.now, soc=soc, grid_power_w=grid_w + charge_w,
            load_power_w=load_w, activity=activity, healthy=healthy,
            reported_charge_power_w=reported_charge_w,
            delivery_topology="mains_isolated",
        )

    def idle(self, soc: float, minutes: int = 15, unrelated_w: float = 50) -> None:
        for _ in range(minutes):
            self.sample("idle", soc, unrelated_w=unrelated_w)

    def cycle(self, *, unrelated_after_w: float = 50, charge_extra_w: float = 0) -> None:
        self.idle(20)
        for minute in range(60):
            self.sample(
                "charging", 20 + (minute + 1), charge_w=1000 + charge_extra_w,
                reported_charge_w=1000,
            )
        self.idle(80, unrelated_w=unrelated_after_w)
        for minute in range(180):
            self.sample("discharging", 80 - (minute + 1) / 3)
        self.idle(20)


def test_three_independent_matching_soc_windows_promote_weighted_efficiency() -> None:
    trace = Trace()
    trace.cycle()
    first = trace.model.summary(trace.now)
    assert first["sample_count"] == 1
    assert first["efficiency_pct"] == pytest.approx(75, abs=1)
    assert first["planning_efficiency_pct"] is None
    trace.cycle()
    assert trace.model.summary(trace.now)["planning_efficiency_pct"] is None
    trace.cycle()
    result = trace.model.summary(trace.now)
    assert result["status"] == "ready"
    assert result["planning_efficiency_pct"] == pytest.approx(75, abs=1)
    assert result["sample_count"] == 3
    assert result["charge_kwh"] == pytest.approx(3, abs=0.04)
    assert result["discharge_kwh"] == pytest.approx(2.25, abs=0.04)
    windows = result["accepted_windows"]
    assert all(a["end"] <= b["start"] for a, b in pairwise(windows))


@pytest.mark.parametrize("seconds", [60, 77, 110])
def test_energy_integration_uses_elapsed_time_not_a_sixty_second_cap(seconds: int) -> None:
    trace = Trace()
    trace.idle(20)
    count = 60
    for index in range(count):
        trace.sample("charging", 20 + (index + 1), seconds=seconds, charge_w=1000)
    trace.idle(80)
    for index in range(180):
        trace.sample("discharging", 80 - (index + 1) / 3, seconds=seconds)
    trace.idle(20)
    result = trace.model.summary(trace.now)
    assert result["sample_count"] == 1
    assert result["efficiency_pct"] == pytest.approx(75, abs=1.5)
    assert result["charge_kwh"] == pytest.approx(count * seconds / 3600, abs=0.04)


def test_changed_other_household_load_rejects_the_charge_baseline() -> None:
    trace = Trace()
    trace.cycle(unrelated_after_w=350)
    assert trace.model.summary(trace.now)["sample_count"] == 0


def test_unrelated_appliance_during_charge_is_rejected_by_corroboration() -> None:
    trace = Trace()
    trace.cycle(charge_extra_w=400)
    assert trace.model.summary(trace.now)["sample_count"] == 0


@pytest.mark.parametrize("failure", ["gap", "unhealthy", "unknown", "export", "nan"])
def test_bad_measurement_invalidates_the_whole_in_flight_window(failure: str) -> None:
    trace = Trace()
    trace.idle(20)
    for index in range(60):
        trace.sample("charging", 20 + index + 1, charge_w=1000)
    trace.idle(80)
    kwargs = {"soc": 80, "grid_power_w": 50, "load_power_w": 250,
              "activity": "discharging", "healthy": True}
    if failure == "gap":
        trace.now += timedelta(minutes=30)
    elif failure == "unhealthy":
        kwargs["healthy"] = False
    elif failure == "unknown":
        kwargs["activity"] = "unknown"
    elif failure == "export":
        kwargs["grid_power_w"] = -100
    else:
        kwargs["grid_power_w"] = float("nan")
    trace.now += timedelta(seconds=60)
    trace.model.observe(trace.now, **kwargs)
    for index in range(180):
        trace.sample("discharging", 80 - (index + 1) / 3)
    trace.idle(20)
    assert trace.model.summary(trace.now)["sample_count"] == 0


def test_cycle_with_different_end_soc_never_becomes_a_sample() -> None:
    trace = Trace()
    trace.idle(20)
    for index in range(60):
        trace.sample("charging", 20 + index + 1, charge_w=1000)
    trace.idle(80)
    for index in range(150):
        trace.sample("discharging", 80 - (index + 1) / 3)
    trace.idle(30, minutes=120)
    assert trace.model.summary(trace.now)["sample_count"] == 0


def test_small_soc_excursions_and_tiny_energy_are_not_promoted() -> None:
    trace = Trace()
    for _ in range(4):
        trace.idle(20)
        for index in range(10):
            trace.sample("charging", 20 + index + 1, charge_w=1000)
        trace.idle(30)
        for index in range(30):
            trace.sample("discharging", 30 - (index + 1) / 3)
        trace.idle(20)
    assert trace.model.summary(trace.now)["planning_efficiency_pct"] is None


def test_restart_retains_completed_windows_but_discards_offline_energy() -> None:
    trace = Trace()
    trace.cycle()
    trace.idle(20)
    for index in range(60):
        trace.sample("charging", 20 + index + 1, charge_w=1000)
    saved = json.loads(json.dumps(trace.model.as_dict(), allow_nan=False))
    trace.model = MatchedSocEfficiency.from_dict(saved)
    trace.now += timedelta(hours=5)
    trace.idle(80)
    for index in range(180):
        trace.sample("discharging", 80 - (index + 1) / 3)
    trace.idle(20)
    assert trace.model.summary(trace.now)["sample_count"] == 1


def test_source_changes_and_old_evidence_restore_the_fallback() -> None:
    trace = Trace()
    for _ in range(3):
        trace.cycle()
    assert trace.model.summary(trace.now)["planning_efficiency_pct"] is not None
    trace.model.prune(trace.now + timedelta(days=31))
    assert trace.model.summary(trace.now + timedelta(days=31))["sample_count"] == 0
    trace.cycle()
    trace.model.configure("different-phase-or-capacity")
    assert trace.model.summary(trace.now)["sample_count"] == 0


def test_malformed_persistence_cannot_supply_a_planning_efficiency() -> None:
    model = MatchedSocEfficiency.from_dict({
        "source": "phase-l1", "accepted_windows": [None, {}, {"charge_kwh": "bad"}],
    })
    result = model.summary(datetime(2026, 10, 1, tzinfo=UTC))
    assert result["planning_efficiency_pct"] is None
    json.dumps(model.as_dict(), allow_nan=False)


def test_full_soc_discharge_first_windows_are_supported() -> None:
    trace = Trace()
    trace.idle(100)
    for index in range(180):
        trace.sample("discharging", 100 - (index + 1) / 3)
    trace.idle(40)
    for index in range(60):
        trace.sample("charging", 40 + index + 1, charge_w=1000)
    trace.idle(100)
    result = trace.model.summary(trace.now)
    assert result["sample_count"] == 1
    assert result["efficiency_pct"] == pytest.approx(75, abs=1)


def test_completed_baseline_audit_survives_restart_unchanged() -> None:
    trace = Trace()
    trace.cycle()
    saved = trace.model.as_dict()
    restored = MatchedSocEfficiency.from_dict(json.loads(json.dumps(saved)))
    assert restored.as_dict() == saved
    assert restored.summary(trace.now)["accepted_windows"][0]["charge_events"]


def test_matched_cycle_retains_duration_power_and_topology_for_loss_analysis():
    trace = Trace()
    trace.cycle()
    window = trace.model.summary(trace.now)["accepted_windows"][0]
    context = window["measurement_context"]
    assert context["delivery_topology"] == "mains_isolated"
    assert context["charge_seconds"] == 3600
    assert context["discharge_seconds"] == 10800
    assert context["charge_mean_load_w"] == 250
    assert context["discharge_mean_load_w"] == 250
    assert context["charge_mean_input_w"] == pytest.approx(1000)
    assert context["discharge_load_min_w"] == context["discharge_load_max_w"] == 250
    assert (window["soc_min"], window["soc_max"]) == (20, 80)
    restored = MatchedSocEfficiency.from_dict(json.loads(json.dumps(trace.model.as_dict())))
    assert restored.accepted_windows[0]["measurement_context"] == context


@pytest.mark.parametrize("bad_context", [
    {"delivery_topology": []},
    {"delivery_topology": "mains_isolated", "charge_seconds": float("nan")},
])
def test_invalid_optional_loss_metadata_does_not_destroy_qualified_cycle(bad_context):
    trace = Trace()
    trace.cycle()
    saved = trace.model.as_dict()
    saved["accepted_windows"][0]["measurement_context"] = bad_context
    restored = MatchedSocEfficiency.from_dict(saved)
    assert len(restored.accepted_windows) == 1
    assert "measurement_context" not in restored.accepted_windows[0]


def test_unsettled_idle_soc_cannot_establish_a_charge_baseline() -> None:
    trace = Trace()
    for minute in range(15):
        trace.sample("idle", 20 + minute / 2)
    for index in range(60):
        trace.sample("charging", 27 + index + 1, charge_w=1000)
    trace.idle(87)
    for index in range(180):
        trace.sample("discharging", 87 - (index + 1) / 3)
    trace.idle(27)
    assert trace.model.summary(trace.now)["sample_count"] == 0


def test_fast_polling_still_retains_enough_idle_baseline() -> None:
    trace = Trace()
    for _ in range(150):
        trace.sample("idle", 20, seconds=5)
    for minute in range(60):
        trace.sample("charging", 20 + minute + 1, charge_w=1000)
    trace.idle(80)
    for minute in range(180):
        trace.sample("discharging", 80 - (minute + 1) / 3)
    trace.idle(20)
    assert trace.model.summary(trace.now)["sample_count"] == 1


def test_unstable_power_baseline_is_rejected() -> None:
    trace = Trace()
    for minute in range(20):
        trace.sample("idle", 20, unrelated_w=50 + (minute % 2) * 200)
    for minute in range(60):
        trace.sample("charging", 20 + minute + 1, charge_w=1000)
    trace.idle(80)
    for minute in range(180):
        trace.sample("discharging", 80 - (minute + 1) / 3)
    trace.idle(20)
    assert trace.model.summary(trace.now)["sample_count"] == 0


def test_outlier_cannot_replace_an_established_planning_efficiency() -> None:
    trace = Trace()
    for _ in range(3):
        trace.cycle()
    before = trace.model.summary(trace.now)["planning_efficiency_pct"]
    trace.idle(20)
    for minute in range(60):
        trace.sample("charging", 20 + minute + 1, charge_w=1000)
    trace.idle(80)
    for minute in range(120):
        trace.sample("discharging", 80 - (minute + 1) / 2)
    trace.idle(20)
    result = trace.model.summary(trace.now)
    assert result["sample_count"] == 3
    assert result["planning_efficiency_pct"] == before


def test_stable_serialized_evidence_without_a_source_cannot_promote() -> None:
    trace = Trace()
    for _ in range(3):
        trace.cycle()
    saved = trace.model.as_dict()
    saved["source"] = ""
    restored = MatchedSocEfficiency.from_dict(saved)
    result = restored.summary(trace.now)
    assert result["status"] == "not_configured"
    assert result["planning_efficiency_pct"] is None


def test_partial_corroboration_is_rejected() -> None:
    trace = Trace()
    trace.idle(20)
    for minute in range(60):
        trace.sample("charging", 20 + minute + 1, charge_w=1000,
                     reported_charge_w=1000 if minute < 30 else None)
    trace.idle(80)
    for minute in range(180):
        trace.sample("discharging", 80 - (minute + 1) / 3)
    trace.idle(20)
    assert trace.model.summary(trace.now)["sample_count"] == 0
