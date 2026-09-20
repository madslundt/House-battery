"""Small, fail-closed local TCP protocol adapter for AECC FBP1200 devices.

The protocol is newline-delimited JSON.  This module intentionally exposes
only the telemetry read and control-register operations used by the optimizer.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any

DEFAULT_PORT = 8080
REG_MIN_SOC = "3023"
REG_MAX_SOC = "3024"
REG_EMS_ENABLE = "3000"
REG_SCHEDULE_MODE = "3020"
REG_AI_SMART_CHARGE = "3021"
REG_AI_SMART_DISCHARGE = "3022"
REG_CUSTOM_MODE = "3030"
REG_CONTROL_TIME_1 = "3003"
_READ_TIMEOUT_SECONDS = 10
_MAX_FRAME_BYTES = 256 * 1024


class LocalProtocolError(RuntimeError):
    """The battery did not provide a trustworthy local protocol response."""


@dataclass(frozen=True, slots=True)
class FbpLocalSnapshot:
    """Accepted physical values decoded from an EnergyParameter response."""

    soc: float
    charge_power_w: float
    discharge_power_w: float
    grid_power_w: float | None
    pv_power_w: float | None
    raw: dict[str, Any]


class FbpLocalTcpClient:
    """Single-session, serialised JSON-line connection to one FBP1200."""

    def __init__(
        self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._serial = 0
        self._lock = asyncio.Lock()

    async def async_close(self) -> None:
        if self._writer and not self._writer.is_closing():
            self._writer.close()
            await self._writer.wait_closed()
        self._reader = self._writer = None

    async def async_snapshot(self) -> FbpLocalSnapshot:
        response = await self._request({"Get": "EnergyParameter"})
        return decode_energy_parameter(response)

    async def async_read_controls(self) -> dict[str, str]:
        response = await self._request(
            {
                "Get": "Energycontrolparameters",
                "RegControlAddr": [int(REG_MIN_SOC), int(REG_MAX_SOC)],
            }
        )
        return decode_controls(response)

    async def async_set_limits(self, minimum_soc: int, maximum_soc: int) -> None:
        if not 0 <= minimum_soc <= maximum_soc <= 100:
            raise LocalProtocolError("invalid requested SOC limits")
        await self._write_and_verify(
            {REG_MIN_SOC: str(minimum_soc), REG_MAX_SOC: str(maximum_soc)}
        )

    async def async_set_mode(
        self, mode: str, power_w: int, *, min_soc: int, max_soc: int
    ) -> None:
        """Write the proven custom-slot controls; never retry a mutating call."""
        if mode not in {"Charge", "Idle", "Discharge"} or not 0 <= power_w <= 1200:
            raise LocalProtocolError("unsupported local battery command")
        if mode == "Idle":
            slot = f"0,00:00,00:00,0,0,0,0,0,0,{max_soc},{min_soc}"
        else:
            signed_power = -power_w if mode == "Charge" else power_w
            slot = f"1,00:00,23:59,{signed_power},0,6,5,0,0,{max_soc},{min_soc}"
        await self._request(
            {
                "Set": "Energycontrolparameters",
                "SetControlInfo": {
                    REG_EMS_ENABLE: "1",
                    REG_SCHEDULE_MODE: "6",
                    REG_AI_SMART_CHARGE: "0",
                    REG_AI_SMART_DISCHARGE: "0",
                    REG_CUSTOM_MODE: "1",
                    REG_CONTROL_TIME_1: slot,
                },
            }
        )

    async def async_set_self_consumption(self) -> None:
        """Return to the device's local self-consumption / zero-export mode.

        This is the small, allowlisted AI restore sequence used by compatible
        AECC devices. It intentionally does not retry: an ambiguous write is
        always safer than issuing the same battery command again.
        """
        await self._request(
            {
                "Set": "Energycontrolparameters",
                "SetControlInfo": {
                    REG_EMS_ENABLE: "1",
                    REG_SCHEDULE_MODE: "3",
                    REG_AI_SMART_CHARGE: "0",
                    REG_AI_SMART_DISCHARGE: "1",
                    REG_CUSTOM_MODE: "0",
                    REG_CONTROL_TIME_1: "0,00:00,00:00,0,0,0,0,0,0,100,10",
                },
            }
        )

    async def _request(self, command: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            try:
                reader, writer = await self._connection()
                self._serial += 1
                payload = {
                    **command,
                    "SerialNumber": self._serial,
                    "CommandSource": "HA",
                }
                writer.write(
                    json.dumps(payload, separators=(",", ":")).encode() + b"\n"
                )
                await writer.drain()
                response = await self._read_json(reader)
                if not isinstance(response, dict):
                    raise LocalProtocolError("TCP response is not an object")
                return response
            except (
                OSError,
                TimeoutError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                await self.async_close()
                raise LocalProtocolError(f"local TCP request failed: {exc}") from exc

    async def _connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._writer is None or self._writer.is_closing() or self._reader is None:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        return self._reader, self._writer

    async def _read_json(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        """Accept a complete JSON object with or without a trailing newline."""
        buffer = b""
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            while len(buffer) <= _MAX_FRAME_BYTES:
                chunk = await reader.read(4096)
                if not chunk:
                    raise LocalProtocolError("battery closed the TCP connection")
                buffer += chunk
                try:
                    response = json.loads(buffer.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(response, dict):
                    raise LocalProtocolError("TCP response is not an object")
                return response
        raise LocalProtocolError("oversized TCP response")

    async def _write_and_verify(self, values: dict[str, str]) -> None:
        await self._request(
            {"Set": "Energycontrolparameters", "SetControlInfo": values}
        )
        observed = await self.async_read_controls()
        if any(observed.get(key) != value for key, value in values.items()):
            raise LocalProtocolError("native SOC control read-back mismatch")


def decode_energy_parameter(response: dict[str, Any]) -> FbpLocalSnapshot:
    """Decode only conservative, known AECC summary/storage fields."""
    summary = _first_mapping(response.get("SSumInfoList"))
    storage = _first_mapping(response.get("Storage_list"))
    soc = _number(summary, "AverageBatteryAverageSOC") or _number(storage, "BatterySoc")
    if soc is None or not 0 <= soc <= 100:
        raise LocalProtocolError("missing or invalid battery SOC")
    charge = (
        _number(summary, "TotalChargePower")
        or _scaled(storage, "BatteryChargingPower", 0.1)
        or 0.0
    )
    discharge = (
        _number(summary, "TotalBatteryOutputPower")
        or _scaled(storage, "BatteryDischargingPower", 0.1)
        or 0.0
    )
    return FbpLocalSnapshot(
        soc,
        max(0, charge),
        max(0, discharge),
        _number(summary, "MeterTotalActivePower"),
        _number(summary, "TotalPVPower"),
        response,
    )


def decode_controls(response: dict[str, Any]) -> dict[str, str]:
    """Extract allowlisted control values from the protocol's register reply."""
    for value in response.values():
        if isinstance(value, dict) and all(
            key in value for key in (REG_MIN_SOC, REG_MAX_SOC)
        ):
            return {key: str(value[key]) for key in (REG_MIN_SOC, REG_MAX_SOC)}
    if all(key in response for key in (REG_MIN_SOC, REG_MAX_SOC)):
        return {key: str(response[key]) for key in (REG_MIN_SOC, REG_MAX_SOC)}
    raise LocalProtocolError("control read-back lacks SOC registers")


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return value if isinstance(value, dict) else {}


def _number(source: dict[str, Any], key: str) -> float | None:
    try:
        value = float(source[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _scaled(source: dict[str, Any], key: str, scale: float) -> float | None:
    value = _number(source, key)
    return value * scale if value is not None else None
