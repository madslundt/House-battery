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
        "off_grid_load_power_w": None,
    }


def test_rejects_missing_or_impossible_soc() -> None:
    with pytest.raises(LocalProtocolError, match="SOC"):
        decode_energy_parameter({"SSumInfoList": [{"AverageBatteryAverageSOC": 101}]})


def test_retries_the_read_only_startup_snapshot_after_a_stale_socket() -> None:
    class RetryingClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            self.requests = 0
            self.closes = 0

        async def _request(self, command: dict):
            self.requests += 1
            if self.requests == 1:
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
    assert client.requests == 2
    assert client.closes == 1


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
    assert values["3021"] == "0"
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
    accepted = FbpLocalSnapshot(50, 0, 0, None, None, {"Storage_list": [{}]})

    assert validator.validate(accepted, now) is None
    assert "zero" in validator.validate(
        FbpLocalSnapshot(0, 0, 500, None, None, {"Storage_list": [{}]}),
        now + timedelta(minutes=1),
    )
    assert "implausible" in validator.validate(
        FbpLocalSnapshot(90, 0, 0, None, None, {"Storage_list": [{}]}),
        now + timedelta(minutes=1),
    )


def test_telemetry_validator_rejects_transiently_missing_stack_units() -> None:
    validator = FbpTelemetryValidator()
    now = datetime(2026, 9, 20, tzinfo=UTC)
    assert (
        validator.validate(
            FbpLocalSnapshot(50, 0, 0, None, None, {"Storage_list": [{}, {}]}), now
        )
        is None
    )

    assert "lost" in validator.validate(
        FbpLocalSnapshot(50, 0, 0, None, None, {"Storage_list": [{}]}),
        now + timedelta(minutes=1),
    )
