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
from typing import Any, Callable

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
_READ_RETRY_DELAYS_SECONDS = (0.25, 0.75)
_MAX_CONFIRMED_WRITE_ATTEMPTS = 2
_MAX_FRAME_BYTES = 256 * 1024
_MODE_VERIFY_DELAY_SECONDS = 0.5
_GRID_IDLE_ACTIVE_POWER_W = 5.0
_SOC_ZERO_WARMUP_SECONDS = 60
_MAX_SOC_CHANGE_PER_MINUTE = 10.0
_ACTIVE_POWER_W = 50.0


class LocalProtocolError(RuntimeError):
    """The battery did not provide a trustworthy local protocol response."""


@dataclass(frozen=True, slots=True)
class FbpLocalSnapshot:
    """Accepted physical values decoded from an EnergyParameter response.

    Two values are first-class inputs to the canonical power-flow model:

    * ``load_power_w`` — the connected load the battery must serve, taken from
      the complete per-storage off-grid total (``OffGridLoadPower`` in every
      ``Storage_list`` unit). It is the load behind the battery and therefore
      stays present in every mode (grid/battery/charge); a frame missing any
      unit's off-grid reading fails closed to ``None`` instead of understating
      the load. This deliberately replaces the AI smart-load total
      (``TotalSmartLoadElectricalPower``), which can legitimately read 0 W when
      the managed load is idle and so must never be the planning input.
    * ``grid_power_w`` — the signed grid-meter flow (``MeterTotalActivePower``).

    ``charge_power_w`` / ``discharge_power_w`` are the raw device-reported
    values (``TotalChargePower`` / ``TotalBatteryOutputPower``). They are kept
    purely for diagnostics and troubleshooting: they must never feed planning,
    accounting or learning, because the device reports them as if it were the
    sole supplier of the load and so cannot be used to infer battery-to-load
    flow.
    """

    soc: float
    load_power_w: float | None
    grid_power_w: float | None
    charge_power_w: float
    discharge_power_w: float
    raw: dict[str, Any]

    @property
    def raw_reported_charge_power_w(self) -> float:
        """Raw device-reported charge power (diagnostic only)."""
        return self.charge_power_w

    @property
    def raw_reported_output_power_w(self) -> float:
        """Raw device-reported output power (diagnostic only)."""
        return self.discharge_power_w

    @property
    def load_diagnostics(self) -> dict[str, Any]:
        """Expose every distinct local load reading for transparency.

        The meter total, smart-load total and backup/off-grid total represent
        different electrical scopes. Only the complete per-storage off-grid
        total (``off_grid_load_power_total_w``) is the optimizer's
        connected-load input (see :attr:`load_power_w`); the others are kept
        here as diagnostics so the correct source can be verified.
        """
        summary = _first_mapping(self.raw.get("SSumInfoList"))
        backup_reported = _number(summary, "TotalBackUpPower")
        off_grid_total = _off_grid_total(self.raw)
        backup_w = _normalized_backup_power_w(backup_reported, off_grid_total)
        return {
            "meter_total_active_power_w": _number(summary, "MeterTotalActivePower"),
            "smart_load_power_w": _number(summary, "TotalSmartLoadElectricalPower"),
            # Some FBP1200 firmware reports this aggregate at one tenth of the
            # per-storage OffGridLoadPower value. Keep both the normalized W
            # value and raw payload for diagnostics.
            "backup_load_power_w": backup_w,
            "backup_load_power_reported": backup_reported,
            "off_grid_load_power_per_unit_w": _off_grid_per_unit(self.raw),
            "off_grid_load_power_total_w": off_grid_total,
            "off_grid_load_validation_error": self.off_grid_load_validation_error,
        }

    @property
    def off_grid_load_total_w(self) -> float | None:
        """Complete per-storage load, cross-checked against the system total."""
        if self.off_grid_load_validation_error is not None:
            return None
        return _off_grid_total(self.raw)

    @property
    def off_grid_load_validation_error(self) -> str | None:
        """Reject contradictory readings for the same off-grid power scope.

        AECC defines ``TotalBackUpPower`` as the device's total off-grid power
        and ``OffGridLoadPower`` as the per-storage off-grid load, both in W.
        This FBP1200 has reported the aggregate at 0.1 scale, so that correction
        is accepted only when multiplying it by ten agrees with the complete
        per-storage sum. Any other mismatch makes the load unsafe for planning,
        so the caller receives ``None`` until telemetry is internally
        consistent.
        """
        per_storage_total = _off_grid_total(self.raw)
        reported_system_total = _number(
            _first_mapping(self.raw.get("SSumInfoList")), "TotalBackUpPower"
        )
        system_total = _normalized_backup_power_w(
            reported_system_total, per_storage_total
        )
        if per_storage_total is None or system_total is None:
            return None
        if not _power_totals_match(per_storage_total, system_total):
            return (
                "per-storage off-grid total "
                f"({per_storage_total:g} W) disagrees with device total backup "
                f"power ({reported_system_total:g} W reported; "
                f"{system_total:g} W normalized)"
            )
        return None


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
        """Read telemetry, retrying transient failures on fresh TCP sockets.

        Some compatible firmware closes an idle or displaced local session.
        Retrying this read-only operation is safe. Mutating control writes are
        retried only if read-back proves that the first absolute update did not
        apply; an ambiguous result is never blindly replayed.
        """
        return await self._async_read_with_retry(
            {"Get": "EnergyParameter"}, decode_energy_parameter
        )

    async def async_read_controls(self) -> dict[str, str]:
        """Read control state, reconnecting and retrying transient failures."""
        return await self._async_read_with_retry(
            {
                "Get": "Energycontrolparameters",
                "RegControlAddr": [int(register) for register in CONTROL_REGISTERS],
            },
            decode_controls,
        )

    async def _async_read_with_retry(
        self,
        command: dict[str, Any],
        decoder: Callable[[dict[str, Any]], Any],
    ) -> Any:
        """Retry idempotent reads three times, reconnecting between attempts."""
        attempts = len(_READ_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempts):
            try:
                return decoder(await self._request(command))
            except LocalProtocolError:
                await self.async_close()
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(_READ_RETRY_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable")

    async def async_set_limits(self, minimum_soc: int, maximum_soc: int) -> None:
        if not 0 <= minimum_soc <= maximum_soc <= 100:
            raise LocalProtocolError("invalid requested SOC limits")
        await self._write_and_verify(
            {REG_MIN_SOC: str(minimum_soc), REG_MAX_SOC: str(maximum_soc)}
        )

    async def async_set_mode(
        self, mode: str, power_w: int, *, min_soc: int, max_soc: int
    ) -> None:
        """Write the custom-slot controls, retrying only a verified non-apply."""
        if mode not in {"Charge", "Idle", "Discharge"} or not 0 <= power_w <= 1200:
            raise LocalProtocolError("unsupported local battery command")
        if mode == "Idle":
            # Keep the all-day slot enabled at an explicit 0 W. Disabling the
            # slot only changed the reported mode on this FBP1200; a preceding
            # fixed-power charge kept running until another active mode arrived.
            slot = f"1,00:00,23:59,0,0,6,5,0,0,{max_soc},{min_soc}"
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
        compatible devices. The common write path retries only after a successful
        read-back proves the first absolute register update did not apply.
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

    async def async_set_grid_idle(self, minimum_soc: int, maximum_soc: int) -> None:
        """Stop active battery power, then leave the device in grid/Idle mode.

        On this FBP1200, disabling a custom slot can leave an already-running
        fixed-power charge active even though the registers report Idle. When
        physical telemetry shows charge/discharge still flowing, temporarily
        set the native minimum SOC to 100%, enter Self-Gen/Zero Export to cancel
        that flow, then write an active 0 W custom slot before restoring the
        requested SOC limits. If already physically idle, use the shorter path.
        """
        if not 0 <= minimum_soc <= maximum_soc <= 100:
            raise LocalProtocolError("invalid requested SOC limits")
        controls = await self.async_read_controls()
        try:
            snapshot = await self.async_snapshot()
        except LocalProtocolError:
            snapshot = None

        observed_mode = operating_mode_from_controls(controls)
        active = snapshot is None or max(
            snapshot.charge_power_w, snapshot.discharge_power_w
        ) > _GRID_IDLE_ACTIVE_POWER_W

        if observed_mode == "Idle" and not active:
            idle_slot = (
                f"1,00:00,23:59,0,0,6,5,0,0,{maximum_soc},{minimum_soc}"
            )
            expected = {
                REG_EMS_ENABLE: "1",
                REG_CONTROL_TIME_1: idle_slot,
                REG_SCHEDULE_MODE: "6",
                REG_AI_SMART_CHARGE: "0",
                REG_AI_SMART_DISCHARGE: "0",
                REG_MIN_SOC: str(minimum_soc),
                REG_MAX_SOC: str(maximum_soc),
                REG_CUSTOM_MODE: "1",
            }
            if _controls_match(expected, controls):
                return
            await self.async_set_limits(minimum_soc, maximum_soc)
            await self.async_set_mode(
                "Idle", 0, min_soc=minimum_soc, max_soc=maximum_soc
            )
            return

        await self.async_set_limits(100, 100)
        await self.async_set_self_consumption()
        for attempt in range(5):
            snapshot = await self.async_snapshot()
            if max(snapshot.charge_power_w, snapshot.discharge_power_w) <= (
                _GRID_IDLE_ACTIVE_POWER_W
            ):
                break
            if attempt == 4:
                raise LocalProtocolError(
                    "battery power did not stop under the 100% SOC floor; "
                    "left Self-Gen/Zero Export active"
                )
            await asyncio.sleep(2)
        # Leave Self-Gen while its 100% native floor is still active. The custom
        # zero-power slot can already carry the requested bounds; restoring the
        # native limits afterwards does not enable battery output in Idle.
        await self.async_set_mode(
            "Idle", 0, min_soc=minimum_soc, max_soc=maximum_soc
        )
        await self.async_set_limits(minimum_soc, maximum_soc)

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
                LocalProtocolError,
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
        """Write absolute registers; retry only when read-back proves no apply.

        A timeout after sending a SET is ambiguous. Read the device state on a
        fresh connection first: a matching state means the write landed, while
        a complete mismatching read proves it did not. Only the latter allows
        one retry. If read-back itself is unavailable, return the original
        failure without replaying the command.
        """
        request = {"Set": "Energycontrolparameters", "SetControlInfo": values}
        last_mismatch = False
        for attempt in range(_MAX_CONFIRMED_WRITE_ATTEMPTS):
            try:
                await self._request(request)
            except LocalProtocolError as write_error:
                try:
                    observed = await self.async_read_controls()
                except LocalProtocolError:
                    raise write_error
                if _controls_match(values, observed):
                    return
                if attempt + 1 == _MAX_CONFIRMED_WRITE_ATTEMPTS:
                    raise write_error
                await asyncio.sleep(_READ_RETRY_DELAYS_SECONDS[attempt])
                continue

            await asyncio.sleep(_MODE_VERIFY_DELAY_SECONDS)
            observed = await self.async_read_controls()
            if _controls_match(values, observed):
                return
            last_mismatch = True
            if attempt + 1 < _MAX_CONFIRMED_WRITE_ATTEMPTS:
                await asyncio.sleep(_READ_RETRY_DELAYS_SECONDS[attempt])

        if last_mismatch:
            raise LocalProtocolError(f"native {label} control read-back mismatch")
        raise LocalProtocolError(f"native {label} write was not confirmed")


def _controls_match(values: dict[str, str], observed: dict[str, str]) -> bool:
    return all(
        _control_value_matches(key, value, observed.get(key))
        for key, value in values.items()
    )


def decode_energy_parameter(response: dict[str, Any]) -> FbpLocalSnapshot:
    """Decode only conservative, known summary/storage fields."""
    summary = _first_mapping(response.get("SSumInfoList"))
    storage_entries = _storage_entries(response)
    # System summaries are authoritative for a stack.  A multi-unit fallback
    # would silently turn unit 0 into the whole battery, so reject it instead.
    storage = (
        _first_mapping(response.get("Storage_list"))
        if len(storage_entries) <= 1
        else {}
    )
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
        _off_grid_total(response),
        _number(summary, "MeterTotalActivePower"),
        max(0, charge),
        max(0, discharge),
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


def _off_grid_per_unit(response: dict[str, Any]) -> list[float | None]:
    """Per-storage off-grid load in Storage_list order; None per missing unit."""
    units = _storage_entries(response)
    return [
        _number(unit, "OffGridLoadPower") if unit is not None else None
        for unit in units
    ]


def _off_grid_total(response: dict[str, Any]) -> float | None:
    """Sum of every per-storage off-grid reading; None for a partial frame.

    The connected load is the load behind the battery, so it is the complete
    per-storage off-grid total. A partial frame (any unit without the reading)
    must never be reported as a total: returning None makes the caller fail
    closed rather than understate the load.
    """
    per_unit = _off_grid_per_unit(response)
    if not per_unit or not all(value is not None for value in per_unit):
        return None
    return sum(value for value in per_unit if value is not None)


def _power_totals_match(first_w: float, second_w: float) -> bool:
    """Allow normal sensor rounding while detecting meaningful conflicts."""
    tolerance_w = max(5.0, max(abs(first_w), abs(second_w)) * 0.05)
    return abs(first_w - second_w) <= tolerance_w


def _normalized_backup_power_w(
    reported_w: float | None, per_storage_total_w: float | None
) -> float | None:
    """Correct the FBP1200 summary's observed 0.1 scale when corroborated.

    The AECC protocol documents both fields in watts, but this FBP1200 reports
    ``TotalBackUpPower`` one tenth of the complete ``OffGridLoadPower`` sum.
    Apply the scale correction only when multiplying the summary by ten makes
    the two independent readings agree within normal rounding tolerance.
    Otherwise preserve the reported value so real disagreements still fail
    closed.
    """
    if reported_w is None or per_storage_total_w is None:
        return reported_w
    if _power_totals_match(reported_w, per_storage_total_w):
        return reported_w
    scaled_w = reported_w * 10
    if _power_totals_match(scaled_w, per_storage_total_w):
        return scaled_w
    return reported_w


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return value if isinstance(value, dict) else {}


def _storage_units(response: dict[str, Any]) -> list[dict[str, Any]]:
    value = response.get("Storage_list")
    return [unit for unit in value if isinstance(unit, dict)] if isinstance(value, list) else []


def _storage_entries(response: dict[str, Any]) -> list[dict[str, Any] | None]:
    """Preserve every Storage_list position for transparent stack diagnostics."""
    value = response.get("Storage_list")
    if not isinstance(value, list):
        return []
    return [unit if isinstance(unit, dict) else None for unit in value]


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
