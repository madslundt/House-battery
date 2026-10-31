"""Sizing evidence and background work stay separate from battery control."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import CONF_ACCOUNTING_LOAD_POWER, CONF_EFFICIENCY_GRID_POWER
from house_battery.runtime import RuntimeState
from house_battery.sizing import SizingHistory
from test_house_battery_cutoff_gate import coordinator
from test_house_battery_efficiency_coordinator import QualifiedMeasurement


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_restored_sizing_cache_is_recomputed(monkeypatch, invalid):
    async def scenario():
        coord, _, _, now = await _ready()
        assess = Mock(return_value=_result())
        monkeypatch.setattr("house_battery.coordinator.assess_sizing", assess)
        _status(coord, now)
        await coord._sizing_task
        coord.runtime.sizing_assessment["added_annualized_savings_dkk"] = invalid
        response = _status(coord, now)
        assert response["state"] == "learning"
        json.dumps(response, allow_nan=False)
        await coord._sizing_task
        assert assess.call_count == 2
        json.dumps(coord.data["battery_sizing"], allow_nan=False)

    asyncio.run(scenario())


def test_sizing_failure_is_explained_and_retries_without_changing_control(monkeypatch):
    async def scenario():
        coord, client, _, now = await _ready()
        calls = list(client.calls)
        assess = Mock(side_effect=[RuntimeError("sizing fixture failure"), _result()])
        monkeypatch.setattr("house_battery.coordinator.assess_sizing", assess)
        _status(coord, now)
        await coord._sizing_task
        waiting = _status(coord, now)
        assert waiting["state"] == "learning"
        assert "retry" in waiting["reason"]
        assert waiting["next_retry_at"]
        assert assess.call_count == 1
        assert client.calls == calls
        assert coord.data["healthy"]
        _status(coord, now + timedelta(hours=2))
        await coord._sizing_task
        assert assess.call_count == 2
        assert coord.runtime.sizing_assessment["state"] == "size_is_fine"

    asyncio.run(scenario())


@pytest.mark.parametrize("grid_state", ["off", "unavailable"])
def test_outages_and_unknown_grid_availability_do_not_invent_charge_opportunities(monkeypatch, grid_state):
    async def scenario():
        coord, _, hass, _ = await coordinator(override="grid")
        hass.states.async_set("binary_sensor.grid", grid_state)
        observe = Mock(return_value=False)
        monkeypatch.setattr(SizingHistory, "observe", observe)
        await coord._async_update_data()
        assert observe.call_count == 1
        assert observe.call_args.kwargs["load_w"] is None

    asyncio.run(scenario())


def _history(now, *, source="usage-scope", days=28, exclude_weekends=False):
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows = []
    while start < end:
        if not exclude_weekends or start.weekday() < 5:
            rows.append({
                "start": start.isoformat(), "coverage_seconds": 3600,
                "load_wh": 250, "price_seconds": 7200,
            })
        start += timedelta(hours=1)
    return SizingHistory.from_dict({"source": source, "hours": rows})


async def _ready():
    coord, client, hass, _ = await coordinator(override="grid")
    coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.real_power"
    coord.runtime.measured_efficiency = QualifiedMeasurement()
    hass.states.async_set("sensor.real_power", "320", {"unit_of_measurement": "W"})
    initial = await coord._async_update_data()
    coord.async_set_updated_data(initial)
    now = datetime.now(UTC)
    coord.runtime.sizing_history = _history(now, source=coord.runtime.sizing_history.source)
    return coord, client, hass, now


def _status(coord, now, summary=None):
    return coord._battery_sizing_status(
        now, coord._settings(),
        coord._efficiency_summary(now) if summary is None else summary,
    )


def _result(**changes):
    return {"state": "size_is_fine", "reason": "Controlled sizing result", **changes}


def _blocked_executor(monkeypatch, hass):
    """Hold only sizing work; the regular control planner still uses its executor."""
    import house_battery.coordinator as module

    entered, release = asyncio.Event(), asyncio.Event()
    assess = Mock(return_value=_result())
    original = hass.async_add_executor_job

    async def execute(func, *args):
        if getattr(func, "func", None) is assess:
            entered.set()
            await release.wait()
            return func(*args)
        return await original(func, *args)

    monkeypatch.setattr(module, "assess_sizing", assess)
    monkeypatch.setattr(hass, "async_add_executor_job", execute)
    return entered, release, assess


def test_actual_connected_load_is_collected_without_household_accounting_binding(monkeypatch):
    async def scenario():
        coord, _, _, _ = await coordinator(override="grid")
        observations = []
        original = SizingHistory.observe

        def observe(history, now, **values):
            observations.append(values)
            return original(history, now, **values)

        monkeypatch.setattr(SizingHistory, "observe", observe)
        assert not coord.config.get(CONF_ACCOUNTING_LOAD_POWER)
        status = await coord._async_update_data()

        assert observations[-1]["load_w"] == 250
        assert observations[-1]["price"] == 4
        assert observations[-1]["price_start"] is not None
        assert status["battery_sizing"]["state"] == "learning"
        assert coord._sizing_task is None

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["days", "weekends", "samples", "unknown_efficiency", "source"])
def test_incomplete_evidence_never_queues_financial_model(monkeypatch, missing):
    import house_battery.coordinator as module

    async def scenario():
        coord, _, _, now = await _ready()
        summary = coord._efficiency_summary(now)
        if missing == "days":
            coord.runtime.sizing_history = _history(now, days=27)
        elif missing == "weekends":
            coord.runtime.sizing_history = _history(now, days=50, exclude_weekends=True)
        elif missing == "samples":
            summary["sample_count"] = 2
        elif missing == "unknown_efficiency":
            summary.update(status="learning", planning_efficiency_pct=None)
        else:
            summary["source"] = ""
        assess = Mock(return_value=_result())
        monkeypatch.setattr(module, "assess_sizing", assess)

        status = _status(coord, now, summary)
        await asyncio.sleep(0)

        assert status["state"] == "learning"
        assert not status["assessment_ready"]
        assert coord._sizing_task is None
        assess.assert_not_called()

    asyncio.run(scenario())


def test_history_and_assessment_persist_and_old_settings_receive_sizing_defaults():
    now = datetime.now(UTC)
    state = RuntimeState.from_dict({
        "schema_version": 3, "settings": {"capacity_kwh": 3.916},
        "sizing_history": _history(now).as_dict(),
        "sizing_assessment": {**_result(), "request_key": "saved-key"},
    })
    state.settings["sizing_expansion_cost_dkk"] = 0
    restored = RuntimeState.from_dict(state.as_dict())
    restored.prune(now)

    assert restored.settings["capacity_kwh"] == 3.916
    assert restored.settings["sizing_expansion_capacity_kwh"] == 1.958
    assert restored.settings["sizing_expansion_cost_dkk"] == 0
    assert restored.settings["sizing_payback_target_years"] == 10
    assert restored.sizing_history.as_dict() == state.sizing_history.as_dict()
    assert restored.sizing_history.completed_slots(now)[1]["day_count"] == 28
    assert restored.sizing_assessment == state.sizing_assessment
    assert restored.export("Battery", {})["sizing_assessment"]["request_key"] == "saved-key"


def test_ready_sizing_runs_in_executor_and_reuses_cached_assessment(monkeypatch):
    import house_battery.coordinator as module

    async def scenario():
        coord, _, _, now = await _ready()
        main_thread = threading.get_ident()
        worker_threads = []

        def assess(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return _result()

        model = Mock(side_effect=assess)
        monkeypatch.setattr(module, "assess_sizing", model)
        assert not coord.runtime.adaptive_enabled
        status = _status(coord, now)
        task = coord._sizing_task
        assert status["state"] == "learning" and status["assessment_ready"]
        assert task is not None
        await asyncio.wait_for(task, 2)

        cached = _status(coord, now)
        assert cached["state"] == "size_is_fine"
        assert cached["evaluated_at"]
        assert coord.data["battery_sizing"] == cached
        assert len(worker_threads) == 1
        assert worker_threads[0] != main_thread
        assert model.call_count == 1
        cached["reason"] = "Caller mutation"
        assert _status(coord, now)["reason"] == "Controlled sizing result"
        assert coord._sizing_task is None

    asyncio.run(scenario())


def test_slow_sizing_does_not_block_a_normal_control_refresh(monkeypatch):
    async def scenario():
        coord, _, hass, now = await _ready()
        entered, release, assess = _blocked_executor(monkeypatch, hass)
        _status(coord, now)
        task = coord._sizing_task
        await asyncio.wait_for(entered.wait(), 2)
        try:
            status = await asyncio.wait_for(coord._async_update_data(), 2)
            assert status["healthy"]
            assert status["battery_sizing"]["state"] == "learning"
            assert not task.done()
            assess.assert_not_called()
        finally:
            release.set()
            await task
        assert assess.call_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("binding", ["host", "port", "price_entities"])
def test_source_scope_changes_reset_recorded_history(binding):
    async def scenario():
        coord, _, hass, _ = await _ready()
        source = coord.runtime.sizing_history.source
        assert coord.runtime.sizing_history.as_dict()["hours"]
        if binding == "price_entities":
            original = hass.states.get("sensor.prices")
            hass.states.async_set("sensor.new_prices", original.state, dict(original.attributes))
            value = ["sensor.new_prices"]
        else:
            value = "192.0.2.9" if binding == "host" else 9123
        coord.entry.options[binding] = value

        status = await coord._async_update_data()

        assert coord.runtime.sizing_history.source != source
        assert coord.runtime.sizing_history.as_dict()["hours"] == []
        assert status["battery_sizing"]["state"] == "learning"
        assert not status["battery_sizing"]["assessment_ready"]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["settings", "source", "qualification"])
def test_changed_inputs_reject_inflight_sizing_result(monkeypatch, change):
    async def scenario():
        coord, _, hass, now = await _ready()
        entered, release, _ = _blocked_executor(monkeypatch, hass)
        _status(coord, now)
        task = coord._sizing_task
        await asyncio.wait_for(entered.wait(), 2)
        publisher = Mock()
        monkeypatch.setattr(coord, "async_set_updated_data", publisher)
        save = AsyncMock()
        monkeypatch.setattr(coord.store, "save", save)

        if change == "settings":
            await coord.async_set_setting("sizing_expansion_cost_dkk", 5000)
            _status(coord, now)
        elif change == "source":
            coord.entry.options["host"] = "192.0.2.9"
            await coord._async_update_data()
        else:
            coord.runtime.measured_efficiency.qualified = False
            assert _status(coord, now)["state"] == "learning"
        save.reset_mock()
        release.set()
        await task

        assert coord.runtime.sizing_assessment == {}
        publisher.assert_not_called()
        save.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["settings", "source", "qualification"])
def test_changed_inputs_during_storage_await_do_not_publish_stale_result(monkeypatch, change):
    import house_battery.coordinator as module

    async def scenario():
        coord, _, _, now = await _ready()
        monkeypatch.setattr(module, "assess_sizing", Mock(return_value=_result()))
        saving, release = asyncio.Event(), asyncio.Event()

        async def save(runtime):
            saving.set()
            await release.wait()

        monkeypatch.setattr(coord.store, "save", save)
        publisher = Mock()
        monkeypatch.setattr(coord, "async_set_updated_data", publisher)
        _status(coord, now)
        task = coord._sizing_task
        await asyncio.wait_for(saving.wait(), 2)
        if change == "settings":
            # A second setter save can complete while the assessment's save is held.
            monkeypatch.setattr(coord.store, "save", AsyncMock())
            await coord.async_set_setting("sizing_expansion_cost_dkk", 5000)
        elif change == "source":
            monkeypatch.setattr(coord.store, "save", AsyncMock())
            coord.entry.options["host"] = "192.0.2.9"
            await coord._async_update_data()
        else:
            coord.runtime.measured_efficiency.qualified = False
            assert _status(coord, now)["state"] == "learning"
        release.set()
        await task

        publisher.assert_not_called()
        assert coord._sizing_task is None

    asyncio.run(scenario())


def test_unload_cancels_sizing_and_prevents_late_save_or_publication(monkeypatch):
    async def scenario():
        coord, _, hass, now = await _ready()
        entered, release, assess = _blocked_executor(monkeypatch, hass)
        _status(coord, now)
        task = coord._sizing_task
        await asyncio.wait_for(entered.wait(), 2)
        save, publisher = AsyncMock(), Mock()
        monkeypatch.setattr(coord.store, "save", save)
        monkeypatch.setattr(coord, "async_set_updated_data", publisher)

        await asyncio.wait_for(coord.async_prepare_unload(), 2)
        # Unload legitimately saves the safe-stop state; sizing adds no later save.
        safe_stop_saves = save.await_count
        release.set()
        await asyncio.sleep(0)

        assert task.cancelled()
        assert coord._sizing_task is None
        assert coord._unloading
        assert coord.runtime.sizing_assessment == {}
        assess.assert_not_called()
        publisher.assert_not_called()
        assert save.await_count == safe_stop_saves
        _status(coord, now)
        assert coord._sizing_task is None

    asyncio.run(scenario())


def test_unload_cancels_a_sizing_save_without_blocking_safe_stop(monkeypatch):
    import house_battery.coordinator as module

    async def scenario():
        coord, _, _, now = await _ready()
        monkeypatch.setattr(module, "assess_sizing", Mock(return_value=_result()))
        saving, release = asyncio.Event(), asyncio.Event()
        saves = []

        async def save(runtime):
            saves.append(runtime.pending_safe)
            if len(saves) == 1:
                saving.set()
                await release.wait()

        monkeypatch.setattr(coord.store, "save", save)
        publisher = Mock()
        monkeypatch.setattr(coord, "async_set_updated_data", publisher)
        _status(coord, now)
        task = coord._sizing_task
        await asyncio.wait_for(saving.wait(), 2)

        await asyncio.wait_for(coord.async_prepare_unload(), 2)
        safe_stop_saves = len(saves)
        release.set()
        await asyncio.sleep(0)

        assert task.cancelled()
        assert coord._sizing_task is None
        assert not coord.runtime.pending_safe
        assert safe_stop_saves > 1
        assert len(saves) == safe_stop_saves
        publisher.assert_not_called()

    asyncio.run(scenario())
