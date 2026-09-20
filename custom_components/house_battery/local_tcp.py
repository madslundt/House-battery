"""Small, fail-closed local TCP protocol adapter for compatible devices.

The protocol is newline-delimited JSON.  This module intentionally exposes
only the telemetry read and control-register operations used by the optimizer.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime
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
CONTROL_REGISTERS = (
    REG_EMS_ENABLE,
    REG_CONTROL_TIME_1,
    REG_SCHEDULE_MODE,
    REG_AI_SMART_CHARGE,
    REG_AI_SMART_DISCHARGE,
    REG_MIN_SOC,
    REG_MAX_SOC,
    REG_CUSTOM_MODE,
)
_READ_TIMEOUT_SECONDS = 10
_MAX_FRAME_BYTES = 256 * 1024
_MODE_VERIFY_DELAY_SECONDS = 0.5
_SOC_ZERO_WARMUP_SECONDS = 60
_MAX_SOC_CHANGE_PER_MINUTE = 10.0
_ACTIVE_POWER_W = 50.0


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


class FbpTelemetryValidator:
    """Reject telemetry that is demonstrably incompatible with battery physics.

    Rejected snapshots are deliberately not returned as a held current value:
    the coordinator will mark direct telemetry unavailable and fail closed.
    Keeping the last accepted sample here is only evidence for the next
    validation, not permission to plan or account from stale data.
    """

    def __init__(self) -> None:
        self._first_seen_at: datetime | None = None
        self._last_accepted: FbpLocalSnapshot | None = None
        self._last_accepted_at: datetime | None = None

    def validate(self, snapshot: FbpLocalSnapshot, now: datetime) -> str | None:
        if self._first_seen_at is None:
            self._first_seen_at = now

        active_power = max(snapshot.charge_power_w, snapshot.discharge_power_w)
        if snapshot.soc == 0 and active_power > _ACTIVE_POWER_W:
            return "SOC is zero while the battery reports active power"
        if (
            snapshot.soc == 0
            and self._last_accepted is None
            and (now - self._first_seen_at).total_seconds() < _SOC_ZERO_WARMUP_SECONDS
        ):
            return "initial zero SOC is awaiting a confirming frame"

        previous = self._last_accepted
        previous_at = self._last_accepted_at
        if previous is not None and previous_at is not None:
            elapsed_seconds = (now - previous_at).total_seconds()
            if elapsed_seconds >= 1:
                rate = abs(snapshot.soc - previous.soc) / (elapsed_seconds / 60)
                if rate > _MAX_SOC_CHANGE_PER_MINUTE:
                    return f"SOC changed at an implausible {rate:.1f}% per minute"
            previous_units = _storage_units(previous.raw)
            current_units = _storage_units(snapshot.raw)
            if previous_units and len(current_units) < len(previous_units):
                return "Storage_list lost one or more battery units"

        self._last_accepted = snapshot
        self._last_accepted_at = now
        return None


class FbpLocalTcpClient:
    """Single-session, serialised JSON-line connection to one battery."""

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
                "RegControlAddr": [int(register) for register in CONTROL_REGISTERS],
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
        await self._write_and_verify(
            {
                REG_EMS_ENABLE: "1",
                REG_SCHEDULE_MODE: "6",
                REG_AI_SMART_CHARGE: "0",
                REG_AI_SMART_DISCHARGE: "0",
                REG_CUSTOM_MODE: "1",
                REG_CONTROL_TIME_1: slot,
            },
            label="mode",
        )

    async def async_set_self_consumption(self) -> None:
        """Return to the device's local self-consumption / zero-export mode.

        This is the small, allowlisted AI restore sequence used by compatible
        compatible devices. It intentionally does not retry: an ambiguous write is
        always safer than issuing the same battery command again.
        """
        await self._write_and_verify(
            {
                REG_EMS_ENABLE: "1",
                REG_SCHEDULE_MODE: "3",
                # The PS240's exact app-written AI-charge behaviour is not yet
                # captured. Preserve the commissioned local policy until a
                # hardware fixture proves that changing this flag is correct.
                REG_AI_SMART_CHARGE: "0",
                REG_AI_SMART_DISCHARGE: "1",
                REG_CUSTOM_MODE: "0",
                REG_CONTROL_TIME_1: "0,00:00,00:00,0,0,0,0,0,0,100,10",
            },
            label="self-consumption mode",
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

    async def _write_and_verify(self, values: dict[str, str], *, label: str = "SOC") -> None:
        await self._request(
            {"Set": "Energycontrolparameters", "SetControlInfo": values}
        )
        await asyncio.sleep(_MODE_VERIFY_DELAY_SECONDS)
        observed = await self.async_read_controls()
        if any(
            not _control_value_matches(key, value, observed.get(key))
            for key, value in values.items()
        ):
            raise LocalProtocolError(f"native {label} control read-back mismatch")


def decode_energy_parameter(response: dict[str, Any]) -> FbpLocalSnapshot:
    """Decode only conservative, known summary/storage fields."""
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
    """Extract only the control-register allowlist from a register reply."""
    for value in (response, *response.values()):
        if isinstance(value, dict) and all(
            key in value for key in (REG_MIN_SOC, REG_MAX_SOC)
        ):
            return {
                key: str(value[key])
                for key in CONTROL_REGISTERS
                if key in value
            }
    raise LocalProtocolError("control read-back lacks SOC registers")


def operating_mode_from_controls(controls: dict[str, str]) -> str | None:
    """Return the active local mode encoded by the allowlisted controls."""
    if controls.get(REG_EMS_ENABLE) != "1":
        return None
    if controls.get(REG_CUSTOM_MODE) == "1":
        slot = controls.get(REG_CONTROL_TIME_1, "")
        fields = slot.split(",")
        try:
            enabled = fields[0] == "1"
            power = float(fields[3])
        except (IndexError, ValueError):
            return None
        if enabled and power < 0:
            return "Charge"
        if enabled and power > 0:
            return "Discharge"
        return "Idle"
    if controls.get(REG_AI_SMART_DISCHARGE) == "1":
        return "Self-Gen/Zero Export"
    return None


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return value if isinstance(value, dict) else {}


def _storage_units(response: dict[str, Any]) -> list[dict[str, Any]]:
    value = response.get("Storage_list")
    return [unit for unit in value if isinstance(unit, dict)] if isinstance(value, list) else []


def _control_value_matches(key: str, expected: str, observed: str | None) -> bool:
    """Compare slot state semantically but all other controls exactly."""
    if key != REG_CONTROL_TIME_1:
        return observed == expected
    expected_slot = _slot_safety_fields(expected)
    observed_slot = _slot_safety_fields(observed)
    return expected_slot is not None and expected_slot == observed_slot


def _slot_safety_fields(value: str | None) -> tuple[str, str, str, str, str, str] | None:
    if value is None:
        return None
    fields = value.split(",")
    if len(fields) < 11:
        return None
    # The enabled bit, time window, signed power, and SOC bounds establish the
    # device behaviour. Intermediate vendor-specific fields may be normalised.
    return tuple(fields[index].strip() for index in (0, 1, 2, 3, 9, 10))


def _number(source: dict[str, Any], key: str) -> float | None:
    try:
        value = float(source[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _scaled(source: dict[str, Any], key: str, scale: float) -> float | None:
    value = _number(source, key)
    return value * scale if value is not None else None
