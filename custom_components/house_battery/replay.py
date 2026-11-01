"""Bounded immutable input vintages for causal, offline policy evaluation."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .capabilities import ControlCapabilities, DeliveryTopology
from .models import Action, PlannedSlot, PlannerSettings, PriceSlot

SCHEMA_VERSION = 1
MAX_SNAPSHOTS = 256
MAX_ACTUALS = 7 * 96
MAX_SLOTS = 512
MAX_RECORD_BYTES = 256 * 1024
MAX_STATE_BYTES = MAX_SNAPSHOTS * MAX_RECORD_BYTES + 1024 * 1024
RETENTION = timedelta(days=7)
MAX_INPUT_AGE = timedelta(minutes=15)


def timestamp(value: Any) -> datetime:
    """Parse one aware timestamp, normalizing DST folds to UTC."""
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Replay timestamps must be timezone-aware")
    return result.astimezone(UTC)


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value):
        raise ValueError("Replay quantities must be finite numbers")
    return float(value)


def _json(value: Any) -> str:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return timestamp(item).isoformat()
        raise TypeError(f"Unsupported replay value: {type(item)}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode, allow_nan=False)


def restore_settings(data: dict[str, Any]) -> PlannerSettings:
    """Restore a validated settings snapshot without consulting live defaults."""
    values = dict(data)
    if values.get("partial_charge_targets") is not None:
        values["partial_charge_targets"] = tuple(values["partial_charge_targets"])
    if isinstance(values.get("control_capabilities"), dict):
        capabilities = dict(values["control_capabilities"])
        capabilities["topology"] = DeliveryTopology(capabilities["topology"])
        values["control_capabilities"] = ControlCapabilities(**capabilities)
    settings = PlannerSettings(**values)
    settings.validate()
    # Some legacy settings validation predates explicit finite-number checks.
    for value in asdict(settings).values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            _finite(value)
    return settings


def restore_slots(rows: list[dict[str, Any]]) -> list[PriceSlot]:
    """Restore issued price/load pairs, retaining provenance and tariff edges."""
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_SLOTS:
        raise ValueError("Replay needs a bounded issued horizon")
    result = []
    for row in rows:
        values = dict(row)
        values["start"], values["end"] = timestamp(values["start"]), timestamp(values["end"])
        if values.get("tariff_start") is not None:
            values["tariff_start"] = timestamp(values["tariff_start"])
        slot = PriceSlot(**values)
        if (slot.end <= slot.start or slot.source not in {"known", "forecast"}
                or not -20 <= _finite(slot.price) <= 100
                or _finite(slot.expected_load_wh) < 0
                or _finite(slot.uncertainty_dkk_per_kwh) < 0):
            raise ValueError("Invalid replay interval")
        result.append(slot)
    return result


def restore_commitment(row: dict[str, Any] | None) -> PlannedSlot | None:
    """Restore the action already published when a replay segment began."""
    if not row:
        return None
    values = dict(row)
    values["start"], values["end"] = timestamp(values["start"]), timestamp(values["end"])
    values["action"] = Action(values["action"])
    result = PlannedSlot(**values)
    if (result.end <= result.start or not -20 <= _finite(result.price) <= 100
            or not 0 <= _finite(result.soc_start) <= 100 or not 0 <= _finite(result.soc_end) <= 100
            or any(_finite(getattr(result, key)) < 0 for key in (
                "expected_load_wh", "grid_import_wh", "battery_charge_wh", "battery_discharge_wh"))
            or result.charge_target_soc is not None and not 0 < _finite(result.charge_target_soc) <= 100):
        raise ValueError("Invalid replay commitment")
    return result


@dataclass(slots=True)
class ReplayHistory:
    """Canonical JSON records prevent caller mutation of already-issued evidence.

    Bounds apply independently of sampling cadence. Consequently retained input
    coverage can be shorter than seven days; export exposes the actual bounds.
    """

    _records: list[str] = field(default_factory=list, repr=False)
    _actuals: list[str] = field(default_factory=list, repr=False)

    def record(
        self, issued_at: datetime, *, slots: Sequence[PriceSlot], settings: PlannerSettings,
        soc: float, scope: str, current_action: Action = Action.GRID,
        committed_slot: PlannedSlot | None = None,
        terminal_price_floor_dkk_per_kwh: float = 0.0,
        current_discharge_enabled: bool = True,
    ) -> bool:
        """Deduplicate inputs, retaining a fresh renewal at their validity boundary."""
        issued = timestamp(issued_at)
        if self._records and issued <= timestamp(json.loads(self._records[-1])["issued_at"]):
            return False
        self.prune(issued)
        if not isinstance(scope, str) or not scope or len(scope) > 256 or not 0 <= _finite(soc) <= 100:
            return False
        payload = {
            "scope": scope, "soc": soc, "current_action": Action(current_action).value,
            "settings": asdict(settings), "slots": [asdict(s) for s in slots],
            "committed_slot": committed_slot.as_dict() if committed_slot else None,
            "terminal_price_floor_dkk_per_kwh": terminal_price_floor_dkk_per_kwh,
            "current_discharge_enabled": current_discharge_enabled,
        }
        try:
            if (_finite(terminal_price_floor_dkk_per_kwh) < 0
                    or not isinstance(current_discharge_enabled, bool)):
                return False
            restore_settings(payload["settings"])
            restore_slots(payload["slots"])
            restore_commitment(payload["committed_slot"])
            content = json.loads(_json(payload))
            if self._records:
                last = json.loads(self._records[-1])
                last_issued = timestamp(last.pop("issued_at"))
                if issued <= last_issued or last == content and issued - last_issued < MAX_INPUT_AGE:
                    return False
            serialized = _json({"issued_at": issued.isoformat(), **content})
            if len(serialized.encode()) > MAX_RECORD_BYTES:
                return False
        except (TypeError, ValueError, KeyError):
            return False
        self._records.append(serialized)
        self._records = self._records[-MAX_SNAPSHOTS:]
        return True

    def record_actual(self, row: dict[str, Any]) -> bool:
        """Archive completed measured intervals, without inventing missing coverage."""
        try:
            start, end = timestamp(row["start"]), timestamp(row["end"])
            seconds = (end - start).total_seconds()
            load, covered, price = (_finite(row[key]) for key in ("load_wh", "covered_seconds", "price"))
            scope = row.get("scope")
            if (not 0 < seconds <= 3600 or load < 0 or not .9 * seconds <= covered <= seconds
                    or not -20 <= price <= 100 or row.get("quality") != "good"
                    or scope is not None and (not isinstance(scope, str) or len(scope) > 256)):
                return False
            if self._actuals and start < timestamp(json.loads(self._actuals[-1])["end"]):
                return False
            self._actuals.append(_json({"start": start, "end": end, "load_wh": load,
                                       "covered_seconds": covered, "price": price, "quality": "good",
                                       "scope": row.get("scope")}))
            # Actual rows may be restored after all snapshots. Pruning inputs
            # against this earlier end would discard their later vintages.
            self._actuals = [item for item in self._actuals
                             if timestamp(json.loads(item)["end"]) > end - RETENTION][-MAX_ACTUALS:]
            return True
        except (TypeError, ValueError, KeyError):
            return False

    def prune(self, now: datetime) -> None:
        """Enforce time and count limits even when no new usable evidence arrives."""
        cutoff, upper = timestamp(now) - RETENTION, timestamp(now)
        self._records = [item for item in self._records
                         if cutoff <= timestamp(json.loads(item)["issued_at"]) <= upper][-MAX_SNAPSHOTS:]
        self._actuals = [item for item in self._actuals
                         if cutoff < timestamp(json.loads(item)["end"]) <= upper][-MAX_ACTUALS:]

    def as_dict(self) -> dict[str, Any]:
        """Return independent copies suitable for persistence."""
        return {"schema_version": SCHEMA_VERSION,
                "records": [json.loads(item) for item in self._records],
                "actuals": [json.loads(item) for item in self._actuals]}

    def as_storage_dict(self) -> dict[str, Any]:
        """Compress repeated full horizons losslessly for persisted HA state."""
        payload = _json(self.as_dict()).encode()
        if len(payload) > MAX_STATE_BYTES:
            raise ValueError("Replay persistence exceeds its bounded state size")
        return {"schema_version": SCHEMA_VERSION, "encoding": "zlib-base64",
                "payload": base64.b64encode(zlib.compress(payload)).decode("ascii")}

    @classmethod
    def from_dict(cls, data: Any) -> ReplayHistory:
        history = cls()
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return history
        if data.get("encoding") == "zlib-base64":
            try:
                encoded = data["payload"]
                if not isinstance(encoded, str) or len(encoded) > (MAX_STATE_BYTES + 2) // 3 * 4:
                    return history
                compressed = base64.b64decode(encoded, validate=True)
                decoder = zlib.decompressobj()
                payload = decoder.decompress(compressed, MAX_STATE_BYTES + 1)
                if (len(payload) > MAX_STATE_BYTES or not decoder.eof
                        or decoder.unused_data or decoder.unconsumed_tail):
                    return history
                data = json.loads(payload)
                if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                        or "encoding" in data):
                    return history
            except (ValueError, TypeError, KeyError, binascii.Error, zlib.error):
                return history
        elif "encoding" in data:
            return history
        records, actuals = data.get("records", []), data.get("actuals", [])
        for row in records[-MAX_SNAPSHOTS:] if isinstance(records, list) else []:
            try:
                committed = restore_commitment(row.get("committed_slot"))
                history.record(timestamp(row["issued_at"]), slots=restore_slots(row["slots"]),
                               settings=restore_settings(row["settings"]), soc=row["soc"],
                               scope=row["scope"], current_action=Action(row["current_action"]),
                               committed_slot=committed,
                               terminal_price_floor_dkk_per_kwh=row.get("terminal_price_floor_dkk_per_kwh", 0.0),
                               current_discharge_enabled=row.get("current_discharge_enabled", True))
            except (KeyError, TypeError, ValueError):
                continue
        for row in actuals[-MAX_ACTUALS:] if isinstance(actuals, list) else []:
            history.record_actual(row)
        return history

    def export(self) -> dict[str, Any]:
        """Expose retained evidence bounds rather than promising seven-day coverage."""
        return {**self.as_dict(), **self.summary()}

    def summary(self) -> dict[str, Any]:
        """Cheap live status; only decode the endpoints of the bounded archive."""
        return {"snapshot_count": len(self._records), "actual_count": len(self._actuals),
                "first_issued_at": json.loads(self._records[0])["issued_at"] if self._records else None,
                "last_issued_at": json.loads(self._records[-1])["issued_at"] if self._records else None,
                "first_actual_start": json.loads(self._actuals[0])["start"] if self._actuals else None,
                "last_actual_end": json.loads(self._actuals[-1])["end"] if self._actuals else None,
                "max_snapshots": MAX_SNAPSHOTS, "retention_days": RETENTION.days,
                "method": "issued_inputs_with_separate_realized_interval_means"}
