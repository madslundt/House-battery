"""Protocol fixtures for the native FBP1200 local TCP compatibility layer."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.local_tcp import (
    REG_MAX_SOC,
    REG_MIN_SOC,
    FbpLocalTcpClient,
    LocalProtocolError,
    decode_controls,
    decode_energy_parameter,
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
                    "TotalPVPower": 0,
                }
            ],
            "Storage_list": [{"BatterySoc": 0, "BatteryChargingPower": 0}],
        }
    )

    assert snapshot.soc == 63
    assert snapshot.discharge_power_w == 420
    assert snapshot.grid_power_w == 350


def test_rejects_missing_or_impossible_soc() -> None:
    with pytest.raises(LocalProtocolError, match="SOC"):
        decode_energy_parameter({"SSumInfoList": [{"AverageBatteryAverageSOC": 101}]})


def test_extracts_only_complete_soc_control_readback() -> None:
    assert decode_controls({"ControlInfo": {REG_MIN_SOC: 10, REG_MAX_SOC: 95}}) == {
        REG_MIN_SOC: "10",
        REG_MAX_SOC: "95",
    }
    with pytest.raises(LocalProtocolError):
        decode_controls({"ControlInfo": {REG_MIN_SOC: 10}})


def test_self_consumption_command_is_limited_to_known_registers() -> None:
    class RecordingClient(FbpLocalTcpClient):
        def __init__(self) -> None:
            pass

        async def _request(self, command: dict):
            self.command = command
            return {}

    client = RecordingClient()
    import asyncio

    asyncio.run(client.async_set_self_consumption())

    values = client.command["SetControlInfo"]
    assert client.command["Set"] == "Energycontrolparameters"
    assert set(values) == {"3000", "3020", "3021", "3022", "3030", "3003"}
