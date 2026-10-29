"""Safety requests supersede pending control and preserve verified idle state."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.actuator import LocalControlAdapter
from house_battery.const import (
    CONF_GRID_ISOLATION_SWITCH,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    CONF_OPERATING_MODE,
)
from house_battery.local_tcp import (
    REG_CONTROL_TIME_SLOTS,
    SLOT_DISABLED,
    FbpLocalTcpClient,
    LocalProtocolError,
    decode_energy_parameter,
)
from house_battery.models import Action
from house_battery.runtime import RuntimeState


@pytest.mark.parametrize("action", [Action.BATTERY, Action.CHARGE])
def test_safe_supersedes_inflight_and_queued_economic_commands(action):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class Device:
            mode = "Idle"
            first_limits = True

            def __init__(self):
                self.calls = []

            async def async_set_limits(self, minimum, maximum):
                if self.first_limits:
                    self.first_limits = False
                    entered.set()
                    await release.wait()

            async def async_set_mode(self, mode, power, **kwargs):
                self.mode = mode
                self.calls.append(mode)

            async def async_set_grid_idle(self, minimum, maximum):
                self.mode = "Idle"
                self.calls.append("Idle")

        async def save():
            pass

        device = Device()
        state = RuntimeState()
        adapter = LocalControlAdapter(
            SimpleNamespace(), dict, lambda: state, save, lambda: device,
        )
        now = datetime.now(UTC)
        inflight = asyncio.create_task(adapter.async_command(action, now, load_w=250))
        await asyncio.wait_for(entered.wait(), 1)
        queued = asyncio.create_task(adapter.async_command(action, now, load_w=250))
        await asyncio.sleep(0)
        safe = asyncio.create_task(adapter.async_command(Action.SAFE, now))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(inflight, queued, safe)

        assert device.mode == "Idle"
        assert device.calls == ["Idle"]
        assert results[2][0]
        assert not results[0][0] and not results[1][0]
        assert state.last_action == "safe"

    asyncio.run(scenario())


def test_synchronous_revocation_stops_legacy_mode_write_before_safe_is_scheduled():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        states = {
            "number.minimum": SimpleNamespace(
                state="10", attributes={"min": 0, "max": 100, "step": 1},
            ),
            "number.maximum": SimpleNamespace(
                state="90", attributes={"min": 0, "max": 100, "step": 1},
            ),
            "select.mode": SimpleNamespace(state="Idle"),
        }
        modes = []

        class Services:
            async def async_call(self, domain, service, data, **kwargs):
                if domain == "number":
                    states[data["entity_id"]].state = str(data["value"])
                    entered.set()
                    await release.wait()
                else:
                    modes.append(data["option"])
                    states[data["entity_id"]].state = data["option"]

        async def save():
            pass

        settings = {
            CONF_MIN_SOC_CONTROL: "number.minimum",
            CONF_MAX_SOC_CONTROL: "number.maximum",
            CONF_OPERATING_MODE: "select.mode",
        }
        state = RuntimeState()
        adapter = LocalControlAdapter(
            SimpleNamespace(states=states, services=Services()),
            lambda: settings, lambda: state, save,
        )
        command = asyncio.create_task(adapter.async_command(Action.CHARGE, datetime.now(UTC)))
        await asyncio.wait_for(entered.wait(), 1)
        adapter.invalidate_pending_commands()
        release.set()
        success, result = await command

        assert not success and "superseded" in result
        assert modes == []
        assert states["select.mode"].state == "Idle"

    asyncio.run(scenario())


def test_revoked_battery_request_does_not_retry_opening_the_grid_plug(monkeypatch):
    monkeypatch.setattr("house_battery.actuator._GRID_SWITCH_RETRY_DELAY_SECONDS", 0)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        plug = SimpleNamespace(state="on")

        class Services:
            attempts = 0

            async def async_call(self, domain, service, data, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    entered.set()
                    await release.wait()
                    raise RuntimeError("first plug command rejected")
                plug.state = "off"

        async def save():
            pass

        services = Services()
        state = RuntimeState()
        adapter = LocalControlAdapter(
            SimpleNamespace(states={"switch.grid": plug}, services=services),
            lambda: {CONF_GRID_ISOLATION_SWITCH: "switch.grid"},
            lambda: state, save, lambda: SimpleNamespace(),
        )
        command = asyncio.create_task(adapter.async_command(
            Action.BATTERY, datetime.now(UTC), load_w=250,
        ))
        await asyncio.wait_for(entered.wait(), 1)
        adapter.invalidate_pending_commands()
        release.set()
        success, result = await command

        assert not success and "superseded" in result
        assert plug.state == "on"
        assert services.attempts == 1

    asyncio.run(scenario())


class IdleDevice(FbpLocalTcpClient):
    """Device registers and measured battery power at the local TCP seam."""

    def __init__(self, *, per_unit_charge=0, per_unit_output=0):
        self.calls = []
        self.charge = per_unit_charge
        self.output = per_unit_output
        self.controls = {
            "3000": "1", "3003": "1,00:00,23:59,0,0,6,5,0,0,90,10",
            "3020": "6", "3021": "0", "3022": "0", "3030": "1",
            "3023": "10", "3024": "90",
            **{key: SLOT_DISABLED for key in REG_CONTROL_TIME_SLOTS[1:]},
        }

    async def async_read_controls(self):
        return self.controls

    async def async_snapshot(self):
        unit = {"BatterySoc": 10, "OffGridLoadPower": 250}
        if self.charge is not None:
            unit["BatteryChargingPower"] = self.charge * 10
        if self.output is not None:
            unit["BatteryDischargingPower"] = self.output * 10
        return decode_energy_parameter({
            "SSumInfoList": [{"AverageBatteryAverageSOC": 10,
                              "TotalChargePower": 48, "TotalBatteryOutputPower": 0}],
            "Storage_list": [unit],
        })

    async def async_set_limits(self, minimum, maximum):
        self.calls.append(("limits", minimum, maximum))
        self.controls.update({"3023": str(minimum), "3024": str(maximum)})

    async def async_set_self_consumption(self):
        self.calls.append(("self_consumption",))
        self.controls.update({"3030": "0", "3020": "3", "3022": "1"})

    async def async_set_mode(self, mode, power, **kwargs):
        self.calls.append(("mode", mode, power))
        self.controls.update({
            "3003": f"1,00:00,23:59,0,0,6,5,0,0,{kwargs['max_soc']},{kwargs['min_soc']}",
            "3020": "6", "3021": "0", "3022": "0", "3030": "1",
            **{key: SLOT_DISABLED for key in REG_CONTROL_TIME_SLOTS[1:]},
        })


def test_verified_idle_with_zero_per_storage_flow_does_not_raise_soc_to_100():
    device = IdleDevice()
    asyncio.run(device.async_set_grid_idle(10, 90))

    assert device.calls == []
    assert device.controls["3023"] == "10"
    assert device.controls["3024"] == "90"


@pytest.mark.parametrize("charge,output", [(48, 0), (0, 48), (None, 0), (0, None)])
def test_standing_summary_draw_never_hides_active_or_unconfirmed_battery_flow(
    monkeypatch, charge, output,
):
    async def no_sleep(delay):
        pass

    monkeypatch.setattr("house_battery.local_tcp.asyncio.sleep", no_sleep)
    device = IdleDevice(per_unit_charge=charge, per_unit_output=output)

    with pytest.raises(LocalProtocolError, match="battery power did not stop"):
        asyncio.run(device.async_set_grid_idle(10, 90))

    assert device.calls == [("limits", 100, 100), ("self_consumption",)]


def test_real_charge_cancels_once_and_restores_idle_despite_remaining_standby_input():
    class ChargingDevice(IdleDevice):
        async def async_set_self_consumption(self):
            await super().async_set_self_consumption()
            self.charge = 0

    device = ChargingDevice(per_unit_charge=48)

    async def scenario():
        await device.async_set_grid_idle(10, 90)
        await device.async_set_grid_idle(10, 90)

    asyncio.run(scenario())

    assert device.calls == [
        ("limits", 100, 100), ("self_consumption",),
        ("mode", "Idle", 0), ("limits", 10, 90),
    ]
    assert device.controls["3023"] == "10"
    assert device.controls["3024"] == "90"


@pytest.mark.parametrize("slot_state", ["active", "missing"])
def test_standing_draw_requires_confirmed_idle_schedules_before_skipping_cancellation(
    slot_state,
):
    device = IdleDevice()
    if slot_state == "active":
        device.controls["3018"] = "1,00:00,23:59,200,0,6,5,0,0,90,10"
    else:
        device.controls.pop("3018")

    asyncio.run(device.async_set_grid_idle(10, 90))

    assert device.calls == [
        ("limits", 100, 100), ("self_consumption",),
        ("mode", "Idle", 0), ("limits", 10, 90),
    ]


def test_fresh_telemetry_session_waits_for_an_inflight_control_response():
    async def scenario():
        reading_control = asyncio.Event()
        release_control = asyncio.Event()

        class Writer:
            closing = False

            def is_closing(self):
                return self.closing

            def close(self):
                self.closing = True

            async def wait_closed(self):
                pass

            def write(self, data):
                pass

            async def drain(self):
                pass

        class Device(FbpLocalTcpClient):
            first_response = True

            async def _connection(self):
                if self._writer is None:
                    self._reader, self._writer = object(), Writer()
                return self._reader, self._writer

            async def _read_json(self, reader):
                if self.first_response:
                    self.first_response = False
                    reading_control.set()
                    await release_control.wait()
                    return {"ack": True}
                return {"SSumInfoList": [{"AverageBatteryAverageSOC": 50}]}

        device = Device("192.0.2.1")
        control = asyncio.create_task(device._request({"Set": "Energycontrolparameters"}))
        await asyncio.wait_for(reading_control.wait(), 1)
        control_writer = device._writer
        telemetry = asyncio.create_task(device.async_snapshot())
        await asyncio.sleep(0)
        closed_while_reading = control_writer.closing
        release_control.set()
        response, snapshot = await asyncio.gather(control, telemetry)
        await device.async_close()

        assert not closed_while_reading
        assert control_writer.closing  # Fresh telemetry still gets a new session.
        assert response == {"ack": True}
        assert snapshot.soc == 50

    asyncio.run(scenario())


@pytest.mark.parametrize("units", [[{"BatterySoc": 50}], [{"BatterySoc": 50}] * 2])
def test_zero_summary_soc_remains_authoritative_for_single_and_stacked_batteries(units):
    snapshot = decode_energy_parameter({
        "SSumInfoList": [{"AverageBatteryAverageSOC": 0}],
        "Storage_list": units,
    })

    assert snapshot.soc == 0


def test_invalid_frame_cleanup_cannot_overlap_a_new_control_request(monkeypatch):
    monkeypatch.setattr("house_battery.local_tcp._READ_RETRY_DELAYS_SECONDS", (0,))

    async def scenario():
        closing_bad_session = asyncio.Event()
        finish_close = asyncio.Event()
        reading_control = asyncio.Event()
        finish_control = asyncio.Event()

        class Writer:
            def __init__(self, first):
                self.first = first
                self.closing = False

            def is_closing(self):
                return self.closing

            def close(self):
                self.closing = True

            async def wait_closed(self):
                if self.first:
                    closing_bad_session.set()
                    await finish_close.wait()

            def write(self, data):
                pass

            async def drain(self):
                pass

        class Device(FbpLocalTcpClient):
            connections = 0
            responses = 0

            async def _connection(self):
                if self._writer is None or self._writer.is_closing():
                    self.connections += 1
                    self._reader = object()
                    self._writer = Writer(first=self.connections == 1)
                return self._reader, self._writer

            async def _read_json(self, reader):
                self.responses += 1
                if self.responses == 1:
                    return {}  # Complete JSON, invalid physical telemetry.
                if self.responses == 2:
                    reading_control.set()
                    await finish_control.wait()
                    return {"ack": True}
                return {"SSumInfoList": [{"AverageBatteryAverageSOC": 50}]}

        device = Device("192.0.2.1")
        telemetry = asyncio.create_task(device.async_snapshot())
        await asyncio.wait_for(closing_bad_session.wait(), 1)
        control = asyncio.create_task(device._request({"Set": "Energycontrolparameters"}))
        await asyncio.sleep(0)
        overlapping = reading_control.is_set()
        finish_close.set()
        finish_control.set()
        snapshot, response = await asyncio.gather(telemetry, control)
        await device.async_close()

        assert not overlapping
        assert snapshot.soc == 50
        assert response == {"ack": True}

    asyncio.run(scenario())
