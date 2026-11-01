"""Regression coverage for interval energy and scheduled baseline separation."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.coordinator import Fbp1200Coordinator
from house_battery.evidence import EvidenceCollector
from house_battery.learning import LoadBucket, LoadLearner
from house_battery.runtime import RuntimeState

NOW = datetime(2026, 11, 1, 0, tzinfo=UTC)


def _coordinator(learner=None):
    coordinator = SimpleNamespace(
        runtime=RuntimeState(load_learner=learner or LoadLearner()),
        _scheduled_load_wh=lambda start, end: Fbp1200Coordinator._scheduled_load_wh(
            coordinator, start, end
        ),
    )
    return coordinator


def _forecast(coordinator, start, end, now, current_load):
    return Fbp1200Coordinator._forecast_load_wh(
        coordinator, start, end, now, current_load
    )


def _schedule(start, end, watts=1000):
    return {"start": start.isoformat(), "end": end.isoformat(),
            "additional_w": watts, "label": "Appliance"}


def test_hourly_forecast_integrates_all_quarter_hour_profile_buckets():
    learner = LoadLearner()
    start = NOW + timedelta(hours=18)
    learner.recent_w.extend([100] * 8)
    for quarter, watts in enumerate([100, 900, 900, 900]):
        learner.buckets[learner.key(start + timedelta(minutes=15 * quarter))] = (
            LoadBucket(count=12, mean_w=watts)
        )
    assert learner.predict_wh(start, start + timedelta(hours=1), now=NOW) == 520


@pytest.mark.parametrize("date,elapsed_hours", [("2026-03-29", 3), ("2026-10-25", 5)])
def test_interval_integration_uses_elapsed_time_across_local_dst(date, elapsed_hours):
    zone = ZoneInfo("Europe/Copenhagen")
    start = datetime.fromisoformat(date).replace(tzinfo=zone)
    end = start.replace(hour=4)
    learner = LoadLearner(time_zone=zone)
    assert learner.predict_wh(start, end, fallback_w=200, now=start) == (
        elapsed_hours * 200
    )


def test_partial_interval_matches_sum_of_retained_quarters():
    learner = LoadLearner()
    start = NOW + timedelta(minutes=7, seconds=30)
    end = NOW + timedelta(hours=1)
    assert learner.predict_wh(start, end, fallback_w=200, now=NOW) == 175


def test_active_schedule_does_not_reuse_total_demand_as_background():
    coordinator = _coordinator()
    start, end = NOW + timedelta(minutes=1), NOW + timedelta(hours=1)
    coordinator.runtime.scheduled_loads = [_schedule(start, end)]
    assert _forecast(coordinator, NOW, start, NOW, 250) == (250 / 60, 0)
    active = start + timedelta(minutes=20)
    result = _forecast(coordinator, active, end, active, 1250)
    assert result == pytest.approx((250 * 39 / 60, 1000 * 39 / 60))


def test_missing_pre_event_background_withholds_forecast_instead_of_subtracting_promise():
    coordinator = _coordinator()
    coordinator.runtime.scheduled_loads = [_schedule(NOW, NOW + timedelta(hours=1))]
    assert _forecast(coordinator, NOW, NOW + timedelta(minutes=15), NOW, 1250) is None


def test_baseline_hold_survives_restart_and_overlapping_events():
    coordinator = _coordinator()
    start = NOW + timedelta(minutes=1)
    end = NOW + timedelta(hours=1)
    coordinator.runtime.scheduled_loads = [
        _schedule(start, end), _schedule(start + timedelta(minutes=10), end, 500)
    ]
    _forecast(coordinator, NOW, start, NOW, 250)
    _forecast(coordinator, start, end, start, 1250)
    coordinator.runtime.load_learner = LoadLearner.from_dict(
        coordinator.runtime.load_learner.as_dict()
    )
    active = start + timedelta(minutes=20)
    assert _forecast(coordinator, active, active + timedelta(minutes=15), active, 1750) == (
        62.5, 375
    )
    assert _forecast(coordinator, end, end + timedelta(minutes=15), end, 250) == (62.5, 0)


def test_unfulfilled_schedule_never_subtracts_promised_watts_from_background():
    coordinator = _coordinator()
    start, end = NOW + timedelta(minutes=1), NOW + timedelta(hours=1)
    coordinator.runtime.scheduled_loads = [_schedule(start, end)]
    _forecast(coordinator, NOW, start, NOW, 250)
    assert _forecast(coordinator, start, start + timedelta(minutes=15), start, 250) == (
        62.5, 250
    )


def test_active_schedule_can_use_qualified_profile_without_recent_background():
    learner = LoadLearner()
    learner.buckets[learner.key(NOW)] = LoadBucket(count=12, mean_w=250)
    coordinator = _coordinator(learner)
    coordinator.runtime.scheduled_loads = [_schedule(NOW, NOW + timedelta(hours=1))]
    assert _forecast(coordinator, NOW, NOW + timedelta(minutes=15), NOW, 1250) == (
        62.5, 250
    )


def test_pre_event_background_hold_expires_without_inventing_a_background():
    coordinator = _coordinator()
    start, end = NOW + timedelta(minutes=1), NOW + timedelta(days=2)
    coordinator.runtime.scheduled_loads = [_schedule(start, end)]
    _forecast(coordinator, NOW, start, NOW, 250)
    later = NOW + timedelta(hours=25)
    assert _forecast(coordinator, later, later + timedelta(minutes=15), later, 1250) is None


def test_scheduled_energy_uses_elapsed_time_across_fall_back():
    zone = ZoneInfo("Europe/Copenhagen")
    start = datetime(2026, 10, 25, 0, tzinfo=zone)
    end = start.replace(hour=4)
    coordinator = _coordinator()
    coordinator.runtime.scheduled_loads = [_schedule(start, end)]
    assert coordinator._scheduled_load_wh(start, end) == 5000


def test_baseline_status_is_read_only_and_timezone_changes_reset_hold():
    learner = LoadLearner()
    start, end = NOW + timedelta(minutes=1), NOW + timedelta(days=2)
    schedules = [_schedule(start, end)]
    assert learner.baseline_status(start, schedules) == "scheduled_baseline_unavailable"
    learner.baseline_fallback(NOW, 250, schedules)
    learner.baseline_fallback(start, 1250, schedules)
    before = learner.as_dict()
    assert learner.baseline_status(start, schedules) == "baseline_available"
    assert learner.baseline_status(NOW + timedelta(hours=25), schedules) == "scheduled_baseline_stale"
    assert learner.as_dict() == before
    learner.configure_time_zone(ZoneInfo("Europe/Copenhagen"))
    assert not learner.baseline_context
    assert learner.baseline_status(start, schedules) == "scheduled_baseline_unavailable"


def test_coordinator_keeps_hourly_tariff_but_integrates_quarter_hour_load():
    from homeassistant.core import HomeAssistant
    from house_battery.const import CONF_LOAD_POWER, CONF_PRICE_ENTITIES

    class Entry:
        entry_id = "load-integration-test"
        title = "Load integration"

        def __init__(self):
            self.data = {CONF_LOAD_POWER: "sensor.load", CONF_PRICE_ENTITIES: ["sensor.prices"]}
            self.options = {}

        def async_on_unload(self, callback):
            pass

    async def scenario():
        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(hass, Entry())
        hass.states.async_set("sensor.load", "100")
        hass.states.async_set("sensor.prices", "1", {"prices": [
            {"start": (NOW + timedelta(hours=index)).isoformat(),
             "end": (NOW + timedelta(hours=index + 1)).isoformat(), "price": 1}
            for index in range(19)
        ]})
        learner = coordinator.runtime.load_learner
        learner.recent_w.extend([100] * 8)
        start = NOW + timedelta(hours=18)
        for quarter, watts in enumerate([100, 900, 900, 900]):
            learner.buckets[learner.key(start + timedelta(minutes=quarter * 15))] = (
                LoadBucket(count=12, mean_w=watts)
            )
        slot = coordinator._price_slots(NOW)[18]
        assert slot.start == start
        assert slot.end == start + timedelta(hours=1)
        assert slot.expected_load_wh == 520

    asyncio.run(scenario())


def test_active_schedule_is_excluded_from_baseline_training_even_after_expiry():
    async def scenario():
        runtime = RuntimeState()
        start, end = NOW + timedelta(minutes=1), NOW + timedelta(minutes=20)
        runtime.scheduled_loads = [_schedule(start, end)]

        async def save():
            pass

        collector = EvidenceCollector(lambda: runtime, lambda: None, save, lambda: 250)
        await collector._learn_load(NOW, 250)
        for minute in range(1, 21):
            await collector._learn_load(NOW + timedelta(minutes=minute), 1250)
        runtime.scheduled_loads.clear()
        for minute in range(21, 46):
            await collector._learn_load(NOW + timedelta(minutes=minute), 250)
        assert runtime.load_learner.buckets
        assert all(bucket.mean_w == pytest.approx(250)
                   for bucket in runtime.load_learner.buckets.values())

    asyncio.run(scenario())
