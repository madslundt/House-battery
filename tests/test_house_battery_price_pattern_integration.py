"""Coordinator coverage for price-pattern precedence, learning and persistence."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

import house_battery.coordinator as coordinator_module
from house_battery.const import (
    CONF_LOAD_POWER,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITIES,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.models import PriceSlot
from house_battery.pattern_models import CONTROL_MODEL, LEAD_BANDS, MODEL_NAMES
from house_battery.runtime import RuntimeState
from test_house_battery_price_events import setup as setup_price_events

NOW = datetime(2026, 10, 26, 12, 7, tzinfo=UTC)
ANCHOR = NOW.replace(minute=0)


class _Entry:
    entry_id = "pattern-integration"
    title = "Pattern integration"

    def __init__(self) -> None:
        self.data = {
            CONF_PRICE_ENTITIES: ["sensor.import_price"],
            CONF_LOAD_POWER: "sensor.connected_load",
            CONF_PRICE_FORECAST_ENTITIES: ["sensor.forecast"],
        }
        self.options = {}

    def async_on_unload(self, callback: object) -> None:
        del callback


def _rows(start: datetime, count: int, *, minutes: int = 15, price: float = 2) -> list[dict]:
    return [{"start": (start + timedelta(minutes=index * minutes)).isoformat(),
             "end": (start + timedelta(minutes=(index + 1) * minutes)).isoformat(),
             "price": price} for index in range(count)]


def _make() -> tuple[HomeAssistant, Fbp1200Coordinator]:
    hass = HomeAssistant("/tmp")
    hass.states.async_set("sensor.connected_load", "100")
    hass.states.async_set("sensor.import_price", "2", {
        "prices": _rows(ANCHOR, 8), "unit_of_measurement": "DKK/kWh",
    })
    coordinator = Fbp1200Coordinator(hass, _Entry())
    coordinator._price_slots(NOW)  # Configure the actual source scope first.
    return hass, coordinator


def _history(time_zone: str) -> list[PriceSlot]:
    zone = ZoneInfo(time_zone)
    slots = []
    for hour in range(42 * 24):
        start = ANCHOR - timedelta(days=42) + timedelta(hours=hour)
        local = start.astimezone(zone)
        price = (1 if local.weekday() >= 5 else 3) + (2 if 17 <= local.hour < 21 else 0)
        slots.append(PriceSlot(start, start + timedelta(hours=1), price))
    return slots


def _train(hass: HomeAssistant, coordinator: Fbp1200Coordinator) -> None:
    history = _history(hass.config.time_zone)
    learner = coordinator.runtime.price_patterns
    for day in range(42):
        end = history[(day + 1) * 24 - 1].end
        learner.observe(history[day * 24:(day + 1) * 24], end)
        local = end.astimezone(ZoneInfo(hass.config.time_zone))
        price = (1 if local.weekday() >= 5 else 3) + (2 if 17 <= local.hour < 21 else 0)
        learner.shadow_issue([PriceSlot(end, end + timedelta(hours=1), price)], end)
    assert coordinator.runtime.price_patterns.summary(NOW)["status"] == "ready"


def test_disabled_forecasting_collects_confirmed_elapsed_prices_without_using_patterns() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        hass.states.async_set("sensor.import_price", "2", {
            "prices": _rows(ANCHOR - timedelta(hours=2), 16),
            "unit_of_measurement": "DKK/kWh",
        })
        result = coordinator._price_slots(NOW)
        assert coordinator.runtime.price_patterns.summary(NOW)["observed_hours"] == 2
        assert len(result) == 8 and all(slot.source == "known" for slot in result)
        _train(hass, coordinator)
        result = coordinator._price_slots(NOW)
        assert len(result) == 8 and coordinator._pattern_used_slots == 0
    asyncio.run(scenario())


def test_known_external_and_pattern_prices_keep_their_precedence_and_provenance() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        coordinator.runtime.forecast_enabled = True
        known_end = ANCHOR + timedelta(hours=2)
        hass.states.async_set("sensor.forecast", "available", {
            "prices": _rows(known_end, 3, minutes=60, price=9),
        })
        result = coordinator._price_slots(NOW)
        assert all(slot.source == "known" and slot.price == 2 for slot in result[:8])
        assert all(slot.source == "forecast" and slot.price == 9 for slot in result[8:11])
        assert all(slot.source == "forecast" and slot.price < 9 for slot in result[11:])
        assert result[11].start == known_end + timedelta(hours=3)
        assert result[-1].end == ANCHOR + timedelta(hours=72)
        assert result[-1].end <= NOW + timedelta(hours=72)
        assert coordinator._forecast_planning_source == "sensor.forecast"
        assert coordinator._forecast_sources["sensor.forecast"]["status"] == "used"
        assert coordinator._forecast_sources["learned_pattern"]["status"] == "used"
        assert coordinator._forecast_sources["learned_pattern"]["evidence"]["status"] == "ready"
        assert coordinator._forecast_used_slot_count == len(result) - 8
    asyncio.run(scenario())


def test_pattern_tail_is_stable_within_quarter_hour_and_advances_at_tariff_boundary() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        coordinator.runtime.forecast_enabled = True
        first = coordinator._price_slots(NOW)
        later = coordinator._price_slots(NOW + timedelta(minutes=4))
        assert first[8:] == later[8:]
        assert first[-1].end == later[-1].end
        boundary = coordinator._price_slots(ANCHOR + timedelta(minutes=15))
        assert boundary[-1].end == first[-1].end + timedelta(minutes=15)
        assert coordinator._forecast_planning_source == "learned_pattern"
        assert coordinator._forecast_status == "used"
    asyncio.run(scenario())


@pytest.mark.parametrize("missing_current", [False, True])
def test_pattern_model_does_not_fill_missing_current_price_or_known_interior_gap(
    missing_current: bool,
) -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        coordinator.runtime.forecast_enabled = True
        rows = _rows(ANCHOR, 8)
        rows.pop(0 if missing_current else 4)
        hass.states.async_set("sensor.import_price", "2", {
            "prices": rows, "unit_of_measurement": "DKK/kWh",
        })
        result = coordinator._price_slots(NOW)
        assert coordinator._pattern_used_slots == 0
        assert all(slot.source == "known" for slot in result)
    asyncio.run(scenario())


def test_forecast_flagged_price_entity_never_becomes_pattern_training_data() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        hass.states.async_set("sensor.import_price", "2", {
            "prices": _rows(ANCHOR - timedelta(hours=2), 16),
            "forecast_data": True, "unit_of_measurement": "DKK/kWh",
        })
        coordinator.runtime.forecast_enabled = True
        result = coordinator._price_slots(NOW)
        assert result == []
        assert not coordinator.runtime.price_patterns.samples
        assert coordinator._pattern_used_slots == 0
    asyncio.run(scenario())


def test_runtime_storage_and_export_keep_pattern_evidence() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        coordinator.runtime.forecast_enabled = True
        stored = coordinator.runtime.as_dict()
        restored = RuntimeState.from_dict(stored)
        assert restored.price_patterns.as_dict() == coordinator.runtime.price_patterns.as_dict()
        exported = restored.export("Pattern integration", {})
        assert exported["forecast"]["patterns"] == stored["price_patterns"]
        coordinator.runtime = restored
        assert coordinator._price_slots(NOW)[-1].end == ANCHOR + timedelta(hours=72)
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["removed", "unavailable"])
def test_temporary_missing_feed_preserves_patterns_but_changed_units_reset_them(missing: str) -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        before = coordinator.runtime.price_patterns.as_dict()
        if missing == "removed":
            hass.states.async_remove("sensor.import_price")
        else:
            hass.states.async_set("sensor.import_price", "unavailable")
        assert coordinator._price_slots(NOW) == []
        assert coordinator.runtime.price_patterns.as_dict() == before
        hass.states.async_set("sensor.import_price", "2", {
            "prices": _rows(ANCHOR, 8), "unit_of_measurement": "EUR/kWh",
        })
        coordinator._price_slots(NOW)
        assert not coordinator.runtime.price_patterns.samples
        assert coordinator.runtime.price_patterns.source != before["source"]
    asyncio.run(scenario())


def test_changed_units_cannot_reseed_patterns_from_old_sizing_history() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        history = coordinator.runtime.sizing_history
        history.configure(coordinator._sizing_source())
        history._hours = {
            int(slot.start.timestamp()): {
                "coverage_seconds": 3600, "price_seconds": slot.price * 3600,
                "load_wh": 100, "covered_until": slot.end.timestamp(),
            } for slot in _history(hass.config.time_zone)
        }
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.import_price", "2", {
            "prices": _rows(ANCHOR, 8), "unit_of_measurement": "EUR/kWh",
        })
        result = coordinator._price_slots(NOW)
        assert not coordinator.runtime.price_patterns.samples
        assert not history._hours and history.source == ""
        assert len(result) == 8 and coordinator._pattern_used_slots == 0
    asyncio.run(scenario())


def test_changed_configured_entity_ids_reset_history_even_while_feed_is_missing() -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        _train(hass, coordinator)
        before = coordinator.runtime.price_patterns.source
        hass.states.async_remove("sensor.import_price")
        coordinator.entry.data[CONF_PRICE_ENTITIES] = ["sensor.new_missing_price"]
        assert coordinator._price_slots(NOW) == []
        assert not coordinator.runtime.price_patterns.samples
        assert coordinator.runtime.price_patterns.source != before
    asyncio.run(scenario())


@pytest.mark.parametrize("compatible", [True, False])
def test_only_compatible_sizing_history_can_warm_start_patterns(compatible: bool) -> None:
    async def scenario() -> None:
        hass, coordinator = _make()
        history = coordinator.runtime.sizing_history
        history.configure(coordinator._sizing_source() if compatible else "different-installation")
        history._hours = {
            int(slot.start.timestamp()): {
                "coverage_seconds": 3600, "price_seconds": slot.price * 3600,
                "load_wh": 100, "covered_until": slot.end.timestamp(),
            } for slot in _history(hass.config.time_zone)
        }
        coordinator.runtime.forecast_enabled = True
        result = coordinator._price_slots(NOW)
        # Warm-started actual prices qualify a profile, not issued forecast
        # skill. Control waits for genuine later outcomes in either case.
        assert coordinator._pattern_used_slots == 0
        assert len(result) == 8
        assert bool(coordinator.runtime.price_patterns.samples) is compatible
        summary = coordinator.runtime.price_patterns.summary(NOW)
        assert summary["history_ready"] is compatible
        assert summary["issued_validation_samples"] == 0
    asyncio.run(scenario())


async def setup_real_price_pattern(monkeypatch, *, sparse_support=False, external_hours=0):
    coord, _, hass, _, clock, optimizer = await setup_price_events(monkeypatch)
    now = datetime(2026, 11, 3, 7, tzinfo=UTC)
    clock[0] = now
    # This fixed historical clock exercises model retention, independently of
    # the real wall-clock freshness of the fixture's otherwise healthy inputs.
    monkeypatch.setattr(coordinator_module, "get_health_problems", lambda *args: [])
    coord.runtime.forecast_enabled = True
    coord.runtime.sizing_history.configure("")
    hass.states.async_set("sensor.prices", "2", {"prices": [
        {"start": (now + timedelta(minutes=15 * index)).isoformat(),
         "end": (now + timedelta(minutes=15 * (index + 1))).isoformat(), "price": 2.}
        for index in range(4)
    ]})
    if external_hours:
        coord.entry.data["price_forecast_entities"] = ["sensor.forecast"]
        hass.states.async_set("sensor.forecast", "ready", {"prices": [
            {"start": (now + timedelta(hours=hour)).isoformat(),
             "end": (now + timedelta(hours=hour + 1)).isoformat(), "price": 2.}
            for hour in range(1, external_hours + 1)
        ]})
    coord._price_slots(now)  # Establish the real learner's configured source.
    learner = coord.runtime.price_patterns
    for hours in range(1, 90 * 24 + 1):
        start = now - timedelta(hours=hours)
        if (sparse_support and start.hour == now.hour and start.weekday() < 5
                and start >= now - timedelta(days=28) and start.weekday() != now.weekday()):
            continue
        learner.samples[start.isoformat()] = (8. if 18 <= start.hour < 21 else 2., None)
    # Real paired evidence qualifies the same control model in every lead band.
    for days in (1, 2, 3):
        for hour in range(8):
            start = (now - timedelta(days=days)).replace(hour=hour)
            for band, lead in zip(LEAD_BANDS, (1, 25, 49), strict=True):
                learner.evidence.scores.append({
                    "start": start.isoformat(),
                    "issued_at": (start - timedelta(hours=lead)).isoformat(),
                    "lead_band": band,
                    "errors": {model: .1 if model == CONTROL_MODEL else 1.
                               for model in MODEL_NAMES},
                })
    return coord, clock, optimizer


@pytest.mark.parametrize("external_hours", [0, 6])
def test_withdrawn_pattern_hour_support_replans_while_selected_models_stay_qualified(
    monkeypatch, external_hours,
):
    async def scenario():
        coord, clock, optimizer = await setup_real_price_pattern(
            monkeypatch, sparse_support=True, external_hours=external_hours,
        )
        initial = await coord._async_update_data()
        original = coord.plan
        selected = coord.runtime.price_patterns.summary(clock[0])["selected_models"]
        calls = optimizer.call_count
        assert initial["system_state"] == "ACTIVE"
        assert coord._pattern_control_status == "active"
        assert coord._pattern_used_slots == 71 - external_hours
        assert original.slots[-1].end == clock[0] + timedelta(hours=72)
        assert selected == dict.fromkeys(LEAD_BANDS, CONTROL_MODEL)

        # At 07:00 the weekday 07:00 bucket has four independent Tuesdays.
        # One minute later its oldest date leaves the 28-day control window.
        # The first unsupported target is tomorrow at 07:00; model-level
        # issued skill remains qualified in all three lead bands.
        clock[0] += timedelta(minutes=1)
        revised = await coord._async_update_data()
        supported_end = coord._last_price_slots[-1].end
        withdrawn = [slot for slot in original.slots if slot.start >= supported_end]
        assert revised["system_state"] == "ACTIVE"
        assert coord._pattern_used_slots == 23 - external_hours
        assert coord.runtime.price_patterns.summary(clock[0])["selected_models"] == selected
        assert supported_end == datetime(2026, 11, 4, 7, tzinfo=UTC)
        assert len(withdrawn) == 48
        assert sum(slot.battery_charge_wh for slot in withdrawn) > 0
        assert sum(slot.battery_discharge_wh for slot in withdrawn) > 0
        assert revised["economic_plan_reused"] is False
        assert revised["economic_plan_last_trigger"] == "model_qualification_changed"
        assert optimizer.call_count > calls
        assert coord.plan is not original
        assert coord.plan.slots[-1].end == supported_end

    asyncio.run(scenario())


def test_real_pattern_numeric_refinements_reuse_plan_when_hour_support_is_unchanged(monkeypatch):
    async def scenario():
        coord, clock, optimizer = await setup_real_price_pattern(monkeypatch)
        initial = await coord._async_update_data()
        original = coord.plan
        previous_slots = [slot for slot in coord._last_price_slots if slot.source == "forecast"]
        learner = coord.runtime.price_patterns
        selected = learner.summary(clock[0])["selected_models"]
        calls = optimizer.call_count
        assert initial["system_state"] == "ACTIVE"
        assert coord._pattern_control_status == "active"
        assert coord._pattern_used_slots == 71

        for stamp, (price, error) in learner.samples.items():
            if price == 8.:
                learner.samples[stamp] = (8.1, error)
        for row in learner.evidence.scores:
            row["errors"][CONTROL_MODEL] = .4
        clock[0] += timedelta(minutes=1)

        refined = await coord._async_update_data()
        current_slots = [slot for slot in coord._last_price_slots if slot.source == "forecast"]
        assert refined["system_state"] == "ACTIVE"
        assert learner.summary(clock[0])["selected_models"] == selected
        assert [(slot.start, slot.end) for slot in current_slots] == [
            (slot.start, slot.end) for slot in previous_slots
        ]
        assert max(slot.price for slot in previous_slots) == 8.
        assert max(slot.price for slot in current_slots) == 8.1
        assert max(slot.uncertainty_dkk_per_kwh for slot in previous_slots) == .25
        assert max(slot.uncertainty_dkk_per_kwh for slot in current_slots) == .4
        assert refined["economic_plan_reused"] is True
        assert coord.plan is original
        assert optimizer.call_count == calls

    asyncio.run(scenario())
