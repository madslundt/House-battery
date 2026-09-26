"""Protocol fixtures for the native FBP1200 local TCP compatibility layer."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.local_tcp import (
    CONTROL_REGISTERS,
    REG_MAX_SOC,
    REG_MIN_SOC,
    FbpLocalSnapshot,
    FbpLocalTcpClient,
    FbpTelemetryValidator,
    LocalProtocolError,
    decode_controls,
    decode_energy_parameter,
    operating_mode_from_controls,
)


def test_decodes_summary_telemetry_without_per_unit_zero_values() -> None:
    snapshot = decode_energy_parameter(
        {
            "SSumInfoList": [
                {
                    "AverageBatteryAverageSOC": 63,
                    "TotalChargePower": 0,
                    "TotalBatteryOutputPower": 420,
                    "MeterTotalActivePower": 350,
                    "TotalSmartLoadElectricalPower": 106,
                    "TotalBackUpPower": 24,
                    "TotalPVPower": 0,
                }
            ],
            "Storage_list": [{"BatterySoc": 0, "BatteryChargingPower": 0}],
        }
    )

    assert snapshot.soc == 63
    assert snapshot.discharge_power_w == 420
    assert snapshot.grid_power_w == 350
    assert snapshot.load_diagnostics == {
        "meter_total_active_power_w": 350,
        "smart_load_power_w": 106,
        "backup_load_power_w": 24,
        "backup_load_power_reported": 24,
        "off_grid_load_power_per_unit_w": [None],
        "off_grid_load_power_total_w": None,
        "off_grid_load_validation_error": None,
    }


def test_load_diagnostics_keeps_every_storage_unit_and_only_aggregates_complete_data() -> None:
    snapshot = decode_energy_parameter(
        {
            "SSumInfoList": [{"AverageBatteryAverageSOC": 63}],
            "Storage_list": [
                {"OffGridLoadPower": 40},
                {"OffGridLoadPower": 66},
            ],
        }
    )

    assert snapshot.load_diagnostics["off_grid_load_power_per_unit_w"] == [40, 66]
    assert snapshot.load_diagnostics["off_grid_load_power_total_w"] == 106

    partial = decode_energy_parameter(
        {
            "SSumInfoList": [{"AverageBatteryAverageSOC": 63}],
            "Storage_list": [{"OffGridLoadPower": 40}, {}],
        }
    )
    assert partial.load_diagnostics["off_grid_load_power_per_unit_w"] == [40, None]
    assert partial.load_diagnostics["off_grid_load_power_total_w"] is None

    malformed = decode_energy_parameter(
        {
            "SSumInfoList": [{"AverageBatteryAverageSOC": 63}],
            "Storage_list": [{"OffGridLoadPower": 40}, None],
        }
    )
    assert malformed.load_diagnostics["off_grid_load_power_per_unit_w"] == [40, None]
    assert malformed.load_diagnostics["off_grid_load_power_total_w"] is None


def test_normalizes_tenth_scale_backup_summary_against_per_storage_total() -> None:
    snapshot = decode_energy_parameter(
        {
            "SSumInfoList": [
                {"AverageBatteryAverageSOC": 63, "TotalBackUpPower": 12.7}
            ],
            "Storage_list": [{"OffGridLoadPower": 127}],
        }
    )

    assert snapshot.load_diagnostics["backup_load_power_w"] == 127
    assert snapshot.load_diagnostics["backup_load_power_reported"] == 12.7
    assert snapshot.load_diagnostics["off_grid_load_power_total_w"] == 127
    assert snapshot.off_grid_load_total_w == 127
    assert snapshot.off_grid_load_validation_error is None


def test_rejects_multi_unit_fallback_without_a_proven_summary() -> None:
    with pytest.raises(LocalProtocolError, match="SOC"):
        decode_energy_parameter(
            {
                "Storage_list": [
                    {"BatterySoc": 50, "BatteryDischargingPower": 8000},
                    {"BatterySoc": 50, "BatteryDischargingPower": 8000},
                ]
            }
        )


def test_rejects_missing_or_impossible_soc() -> None:
    with pytest.raises(LocalProtocolError, match="SOC"):
        decode_energy_parameter({"SSumInfoList": [{"AverageBatteryAverageSOC": 101}]})


def test_retries_the_read_only_startup_snapshot_after_stale_sockets() -> None:
    class RetryingClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            self.requests = 0
            self.closes = 0

        async def _request(self, command: dict):
            self.requests += 1
            if self.requests < 3:
                raise LocalProtocolError("battery closed the TCP connection")
            return {
                "SSumInfoList": [{"AverageBatteryAverageSOC": 28}],
                "Storage_list": [{}],
            }

        async def async_close(self) -> None:
            self.closes += 1

    import asyncio

    client = RetryingClient()
    assert asyncio.run(client.async_snapshot()).soc == 28
    assert client.requests == 3
    assert client.closes == 2


def test_retries_control_reads_after_timeout_but_preserves_last_error() -> None:
    class RetryingClient(FbpLocalTcpClient):
        def __init__(self, failures: int) -> None:
            self.requests = 0
            self.failures = failures
            self.closes = 0

        async def _request(self, command: dict):
            self.requests += 1
            if self.requests <= self.failures:
                raise LocalProtocolError("read timed out")
            return {"ControlInfo": {REG_MIN_SOC: 10, REG_MAX_SOC: 90}}

        async def async_close(self) -> None:
            self.closes += 1

    import asyncio

    recovered = RetryingClient(failures=2)
    assert asyncio.run(recovered.async_read_controls()) == {
        REG_MIN_SOC: "10",
        REG_MAX_SOC: "90",
    }
    assert recovered.requests == 3
    assert recovered.closes == 2

    unavailable = RetryingClient(failures=3)
    with pytest.raises(LocalProtocolError, match="read timed out"):
        asyncio.run(unavailable.async_read_controls())
    assert unavailable.requests == 3
    assert unavailable.closes == 3


def test_timed_out_mode_write_is_not_retried_when_readback_confirms_it_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutWriteClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            self.writes = 0
            self.controls: dict[str, str] = {}

        async def _request(self, command: dict):
            self.writes += 1
            self.controls = command["SetControlInfo"]
            raise LocalProtocolError("write response timed out")

        async def async_close(self) -> None:
            pass

        async def async_read_controls(self) -> dict[str, str]:
            return self.controls

    monkeypatch.setattr("house_battery.local_tcp._MODE_VERIFY_DELAY_SECONDS", 0)
    import asyncio

    client = TimedOutWriteClient()
    asyncio.run(client.async_set_mode("Charge", 200, min_soc=10, max_soc=90))
    assert client.writes == 1


def test_retries_mode_write_only_after_readback_proves_it_was_not_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DroppedFirstWriteClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            self.writes = 0
            self.controls = {
                "3000": "1",
                "3003": "0,00:00,00:00,0,0,0,0,0,0,90,10",
                "3020": "6",
                "3021": "0",
                "3022": "0",
                "3023": "10",
                "3024": "90",
                "3030": "1",
            }

        async def _request(self, command: dict):
            self.writes += 1
            if self.writes > 1:
                self.controls = command["SetControlInfo"]
            return {}

        async def async_read_controls(self) -> dict[str, str]:
            return self.controls

    monkeypatch.setattr("house_battery.local_tcp._MODE_VERIFY_DELAY_SECONDS", 0)
    import asyncio

    client = DroppedFirstWriteClient()
    asyncio.run(client.async_set_mode("Charge", 200, min_soc=10, max_soc=90))
    assert client.writes == 2
    assert operating_mode_from_controls(client.controls) == "Charge"


def test_idle_command_writes_an_active_zero_power_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            pass

        async def _request(self, command: dict):
            self.command = command
            return {}

        async def async_read_controls(self) -> dict[str, str]:
            return self.command["SetControlInfo"]

    monkeypatch.setattr("house_battery.local_tcp._MODE_VERIFY_DELAY_SECONDS", 0)
    import asyncio

    client = RecordingClient()
    asyncio.run(client.async_set_mode("Idle", 0, min_soc=10, max_soc=90))
    values = client.command["SetControlInfo"]
    assert values["3003"] == "1,00:00,23:59,0,0,6,5,0,0,90,10"
    assert operating_mode_from_controls(values) == "Idle"


def test_grid_idle_cancels_active_power_under_a_100_percent_floor() -> None:
    class ActiveChargeClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []
            self.self_consumption = False
            self.controls = {
                "3000": "1",
                "3003": "1,00:00,23:59,-200,0,6,5,0,0,90,10",
                "3020": "6",
                "3021": "0",
                "3022": "0",
                "3023": "10",
                "3024": "90",
                "3030": "1",
            }

        async def async_read_controls(self) -> dict[str, str]:
            return self.controls

        async def async_snapshot(self) -> FbpLocalSnapshot:
            return FbpLocalSnapshot(
                53, 120, 0, 0 if self.self_consumption else 200, 0, {}
            )

        async def async_set_limits(self, minimum_soc: int, maximum_soc: int) -> None:
            self.calls.append(("limits", minimum_soc, maximum_soc))
            self.controls["3023"] = str(minimum_soc)
            self.controls["3024"] = str(maximum_soc)

        async def async_set_self_consumption(self) -> None:
            self.calls.append(("self_consumption",))
            self.self_consumption = True
            self.controls.update({"3020": "3", "3022": "1", "3030": "0"})

        async def async_set_mode(
            self, mode: str, power: int, *, min_soc: int, max_soc: int
        ) -> None:
            self.calls.append(("mode", mode, power, min_soc, max_soc))
            self.controls.update(
                {
                    "3003": f"1,00:00,23:59,0,0,6,5,0,0,{max_soc},{min_soc}",
                    "3020": "6",
                    "3022": "0",
                    "3030": "1",
                }
            )

    import asyncio

    client = ActiveChargeClient()
    asyncio.run(client.async_set_grid_idle(10, 90))

    assert client.calls == [
        ("limits", 100, 100),
        ("self_consumption",),
        ("mode", "Idle", 0, 10, 90),
        ("limits", 10, 90),
    ]
    assert operating_mode_from_controls(client.controls) == "Idle"
    assert client.controls["3023"] == "10"
    assert client.controls["3024"] == "90"


def test_grid_idle_skips_writes_when_mode_limits_and_physical_power_match() -> None:
    class AlreadyIdleClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            self.controls = {
                "3000": "1",
                "3003": "1,00:00,23:59,0,0,6,5,0,0,90,10",
                "3020": "6",
                "3021": "0",
                "3022": "0",
                "3023": "10",
                "3024": "90",
                "3030": "1",
            }
            self.writes: list[str] = []

        async def async_read_controls(self) -> dict[str, str]:
            return self.controls

        async def async_snapshot(self) -> FbpLocalSnapshot:
            return FbpLocalSnapshot(53, 120, 0, 0, 0, {})

        async def async_set_limits(self, minimum_soc: int, maximum_soc: int) -> None:
            self.writes.append("limits")

        async def async_set_mode(
            self, mode: str, power: int, *, min_soc: int, max_soc: int
        ) -> None:
            self.writes.append("mode")

    import asyncio

    client = AlreadyIdleClient()
    asyncio.run(client.async_set_grid_idle(10, 90))

    assert client.writes == []


def test_extracts_only_complete_soc_control_readback() -> None:
    assert decode_controls({"ControlInfo": {REG_MIN_SOC: 10, REG_MAX_SOC: 95}}) == {
        REG_MIN_SOC: "10",
        REG_MAX_SOC: "95",
    }
    with pytest.raises(LocalProtocolError):
        decode_controls({"ControlInfo": {REG_MIN_SOC: 10}})


def test_decodes_the_complete_allowlisted_control_state() -> None:
    controls = decode_controls(
        {
            "ControlInfo": {
                "3000": 1,
                "3003": "1,00:00,23:59,-1200,0,6,5,0,0,90,10",
                "3020": 6,
                "3021": 0,
                "3022": 0,
                "3023": 10,
                "3024": 90,
                "3030": 1,
                "9999": "not allowlisted",
            }
        }
    )

    assert controls == {
        "3000": "1",
        "3003": "1,00:00,23:59,-1200,0,6,5,0,0,90,10",
        "3020": "6",
        "3021": "0",
        "3022": "0",
        "3023": "10",
        "3024": "90",
        "3030": "1",
    }
    assert set(controls) == set(CONTROL_REGISTERS)


def test_decodes_observed_mode_from_live_controls() -> None:
    controls = {
        "3000": "1",
        "3003": "1,00:00,23:59,-500,0,6,5,0,0,90,10",
        "3021": "0",
        "3022": "0",
        "3023": "10",
        "3024": "90",
        "3030": "1",
    }

    assert operating_mode_from_controls(controls) == "Charge"
    controls["3003"] = "1,00:00,23:59,500,0,6,5,0,0,90,10"
    assert operating_mode_from_controls(controls) == "Discharge"
    controls.update({"3030": "0", "3022": "1"})
    assert operating_mode_from_controls(controls) == "Self-Gen/Zero Export"


def test_self_consumption_command_is_verified_and_limited_to_known_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            pass

        async def _request(self, command: dict):
            self.command = command
            return {}

        async def async_read_controls(self) -> dict[str, str]:
            return self.command["SetControlInfo"]

    client = RecordingClient()
    import asyncio

    monkeypatch.setattr("house_battery.local_tcp._MODE_VERIFY_DELAY_SECONDS", 0)
    asyncio.run(client.async_set_self_consumption())

    values = client.command["SetControlInfo"]
    assert client.command["Set"] == "Energycontrolparameters"
    assert set(values) == {"3000", "3020", "3021", "3022", "3030", "3003"}
    # Self-consumption requires both native AI flags.  The FBP1200 accepts and
    # reads back schedule mode 3 with only AI discharge enabled, but remains
    # physically idle under load in that state.
    assert values["3021"] == "1"
    assert values["3022"] == "1"


def test_mode_command_rejects_a_control_readback_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MismatchedClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            pass

        async def _request(self, command: dict):
            return {}

        async def async_read_controls(self) -> dict[str, str]:
            return {"3023": "10", "3024": "90"}

    monkeypatch.setattr("house_battery.local_tcp._MODE_VERIFY_DELAY_SECONDS", 0)
    with pytest.raises(LocalProtocolError, match="mode control read-back mismatch"):
        import asyncio

        asyncio.run(
            MismatchedClient().async_set_mode("Charge", 500, min_soc=10, max_soc=90)
        )


def test_mode_command_accepts_equivalent_normalized_slot_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NormalizingClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            pass

        async def _request(self, command: dict):
            self.command = command
            return {}

        async def async_read_controls(self) -> dict[str, str]:
            values = dict(self.command["SetControlInfo"])
            values["3003"] = values["3003"].replace(",6,5,0,0,", ",6,4,0,0,"
            )
            return values

    monkeypatch.setattr("house_battery.local_tcp._MODE_VERIFY_DELAY_SECONDS", 0)
    import asyncio

    asyncio.run(
        NormalizingClient().async_set_mode("Charge", 500, min_soc=10, max_soc=90)
    )


def test_telemetry_validator_rejects_false_zero_and_implausible_soc_changes() -> None:
    validator = FbpTelemetryValidator()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    accepted = FbpLocalSnapshot(50, 0, None, 0, 0, {"Storage_list": [{}]})

    assert validator.validate(accepted, now) is None
    assert "zero" in validator.validate(
        FbpLocalSnapshot(0, 0, None, 0, 500, {"Storage_list": [{}]}),
        now + timedelta(minutes=1),
    )
    assert "implausible" in validator.validate(
        FbpLocalSnapshot(90, 0, None, 0, 0, {"Storage_list": [{}]}),
        now + timedelta(minutes=1),
    )


def test_telemetry_validator_rejects_transiently_missing_stack_units() -> None:
    validator = FbpTelemetryValidator()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    assert (
        validator.validate(
            FbpLocalSnapshot(50, 0, None, 0, 0, {"Storage_list": [{}, {}]}), now
        )
        is None
    )

    assert "lost" in validator.validate(
        FbpLocalSnapshot(50, 0, None, 0, 0, {"Storage_list": [{}]}),
        now + timedelta(minutes=1),
    )
