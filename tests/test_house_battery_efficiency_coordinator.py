"""Measured efficiency reaches planning only through qualified real-power evidence."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import CONF_EFFICIENCY_GRID_POWER
from house_battery.runtime import RuntimeState
from house_battery.sensor import FbpMeasuredRoundTripEfficiencySensor
from test_house_battery_cutoff_gate import coordinator
from test_house_battery_efficiency import Trace


class QualifiedMeasurement:
    """A model boundary double; the estimator's real windows have separate tests."""

    def __init__(self, *, qualified: bool = True):
        self.qualified = qualified
        self.source = None
        self.samples = []

    def configure(self, source):
        if self.source is not None and self.source != source:
            self.qualified = False
        self.source = source

    def summary(self, now):
        return {
            "source": self.source, "method": "matched_soc_grid_meter",
            "status": "ready" if self.qualified else "insufficient_samples",
            "sample_count": 3 if self.qualified else 1,
            "efficiency_pct": 72.0,
            "planning_efficiency_pct": 72.0 if self.qualified else None,
            "accepted_windows": [],
        }

    def observe(self, now, **values):
        self.samples.append((now, values))
        return True

    def prune(self, now):
        pass

    def as_dict(self):
        return {}


def test_qualified_measurement_drives_settings_and_published_sensor():
    async def scenario():
        coord, _, hass, _ = await coordinator(override="grid")
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l1"
        coord.runtime.settings["round_trip_efficiency"] = 80
        coord.runtime.measured_efficiency = QualifiedMeasurement()
        hass.states.async_set("sensor.phase_l1", "320", {"unit_of_measurement": "W"})
        # Contradictory old evidence must not override the independent meter.
        for _ in range(5):
            coord.runtime.battery_learner.add_efficiency_sample(.95)

        status = await coord._async_update_data()

        assert coord._settings().round_trip_efficiency == pytest.approx(.72)
        assert status["planning_efficiency_source"] == "matched_soc_grid_meter"
        assert status["planning_efficiency_pct"] == 72
        assert status["learned_efficiency_pct"] == 72
        assert status["efficiency_learning_ready"]
        assert status["efficiency_learning_samples"] == 3
        coord.async_set_updated_data(status)
        sensor = FbpMeasuredRoundTripEfficiencySensor(coord)
        assert sensor.native_value == 72
        assert sensor.extra_state_attributes["planning_efficiency_pct"] == 72
        assert "Three consistent" in sensor.extra_state_attributes["note"]

    asyncio.run(scenario())


def test_unqualified_bound_meter_uses_fallback_instead_of_legacy_evidence():
    async def scenario():
        coord, _, _, _ = await coordinator()
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l1"
        coord.runtime.settings["round_trip_efficiency"] = 80
        coord.runtime.measured_efficiency = QualifiedMeasurement(qualified=False)
        for _ in range(5):
            coord.runtime.battery_learner.add_efficiency_sample(.95)
        assert coord._settings().round_trip_efficiency == .8
        assert coord._planning_efficiency()[1] == "configured_fallback"

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["meter", "capacity"])
def test_rebinding_meter_or_changing_capacity_invalidates_planning_evidence(change):
    async def scenario():
        coord, _, _, _ = await coordinator()
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l1"
        coord.runtime.measured_efficiency = QualifiedMeasurement()
        assert coord._settings().round_trip_efficiency == .72
        if change == "meter":
            coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l2"
        else:
            coord.runtime.settings["capacity_kwh"] *= 2
        assert coord._settings().round_trip_efficiency == .85

    asyncio.run(scenario())


def test_missing_optional_efficiency_meter_does_not_pause_battery_control():
    async def scenario():
        coord, _, _, _ = await coordinator(override="battery")
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.not_available"
        model = QualifiedMeasurement(qualified=False)
        coord.runtime.measured_efficiency = model

        status = await coord._async_update_data()

        assert status["healthy"]
        assert status["execution_enabled"]
        assert status["planning_efficiency_source"] == "configured_fallback"
        assert model.samples[-1][1]["grid_power_w"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(("age", "unit", "value", "expected"), [
    (0, "W", "320", 320), (91, "W", "320", None),
    (-10, "W", "320", None), (0, "VA", "320", None),
    (0, "W", "unavailable", None), (0, "W", "nan", None),
])
def test_efficiency_requires_fresh_finite_real_watts(age, unit, value, expected):
    async def scenario():
        coord, _, _, _ = await coordinator()
        now = datetime.now(UTC)
        coord._state = lambda key: SimpleNamespace(
            state=value, attributes={"unit_of_measurement": unit},
            last_updated=now - timedelta(seconds=age),
        )
        assert coord._efficiency_grid_power(now) == expected

    asyncio.run(scenario())


def test_grid_connected_raw_output_is_not_counted_as_delivered_battery_energy():
    async def scenario():
        coord, _, hass, _ = await coordinator()
        hass.states.async_set("switch.grid_input", "on")
        assert coord._efficiency_activity(0, 300) == "unknown"
        hass.states.async_set("switch.grid_input", "off")
        assert coord._efficiency_activity(0, 300) == "discharging"
        assert coord._efficiency_activity(1100, 300) == "unknown"
        assert coord._efficiency_activity(1100, 0) == "unknown"
        hass.states.async_set("switch.grid_input", "on")
        assert coord._efficiency_activity(1100, 0) == "charging"
        assert coord._efficiency_activity(0, 0) == "idle"

    asyncio.run(scenario())


def test_old_runtime_payload_has_no_qualified_grid_measurements():
    runtime = RuntimeState.from_dict({"schema_version": 3})
    assert runtime.measured_efficiency.summary(datetime.now(UTC))["planning_efficiency_pct"] is None
    assert "efficiency_model" in runtime.export("Battery", {})


def test_real_matched_cycles_promote_planner_and_survive_runtime_restart():
    async def scenario():
        coord, _, _, _ = await coordinator()
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l1"
        coord.runtime.settings["round_trip_efficiency"] = 80
        trace = Trace()
        trace.now = datetime.now(UTC) - timedelta(hours=15)
        trace.model.configure(coord._efficiency_source())
        coord.runtime.measured_efficiency = trace.model
        for _ in range(2):
            trace.cycle()
        assert coord._settings().round_trip_efficiency == .8
        trace.cycle()
        assert coord._settings().round_trip_efficiency == pytest.approx(.75, abs=.01)

        payload = coord.runtime.as_dict()
        coord.runtime = RuntimeState.from_dict(payload)
        assert coord._settings().round_trip_efficiency == pytest.approx(.75, abs=.01)
        assert coord._planning_efficiency()[1] == "matched_soc_grid_meter"
        assert len(coord.export_data()["efficiency_model"]["accepted_windows"]) == 3
        coord.runtime.measured_efficiency.prune(datetime.now(UTC) + timedelta(days=31))
        assert coord._settings().round_trip_efficiency == .8

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["safe_stop", "export_latch", "export_now"])
def test_paused_or_exporting_operation_is_not_efficiency_evidence(blocked):
    async def scenario():
        coord, _, _, _ = await coordinator()
        coord.runtime.pending_safe = blocked == "safe_stop"
        coord.runtime.export_safety_fault_latched = blocked == "export_latch"
        coord._float = lambda key, default=None: -100 if blocked == "export_now" else 0
        assert not coord._efficiency_healthy(datetime.now(UTC))

    asyncio.run(scenario())


def test_source_fingerprint_covers_supporting_bindings_without_exporting_host():
    async def scenario():
        coord, _, _, _ = await coordinator()
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l1"
        original = coord._efficiency_source()
        assert original.startswith("sensor.phase_l1:")
        assert coord.entry.data["host"] not in original
        coord.entry.options["grid_import_power_entity"] = "sensor.another_meter"
        assert coord._efficiency_source() != original
        original = coord._efficiency_source()
        coord.entry.options["port"] = 8081
        assert coord._efficiency_source() != original

    asyncio.run(scenario())


def test_meter_reports_arriving_during_tcp_reads_are_fresh_measurements():
    async def scenario():
        coord, client, hass, _ = await coordinator(override="grid")
        coord.entry.options[CONF_EFFICIENCY_GRID_POWER] = "sensor.phase_l1"
        model = QualifiedMeasurement(qualified=False)
        coord.runtime.measured_efficiency = model
        snapshot = client.async_snapshot

        async def read_with_concurrent_meter_report():
            result = await snapshot()
            hass.states.async_set("sensor.phase_l1", "320", {"unit_of_measurement": "W"})
            return result

        client.async_snapshot = read_with_concurrent_meter_report
        await coord._async_update_data()
        observed_at, values = model.samples[-1]
        assert values["grid_power_w"] == 320
        assert observed_at >= hass.states.get("sensor.phase_l1").last_updated

    asyncio.run(scenario())
