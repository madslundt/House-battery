"""Bounded immutable input vintages for causal, offline policy evaluation."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite
from typing import Any

from .capabilities import ControlCapabilities, DeliveryTopology
from .models import Action, PlannedSlot, PlannerSettings, PriceSlot

SCHEMA_VERSION = 2
CHUNK_ENCODING = "zlib-base64-chunks"
RETENTION = timedelta(days=29)
MAX_SNAPSHOTS = 8192
MAX_ACTUALS = RETENTION.days * 96
MAX_SLOTS = 512
MAX_RECORD_BYTES = 256 * 1024
_PLAN_PAGE_SIZE = 16
MAX_STATE_BYTES = _PLAN_PAGE_SIZE * (MAX_RECORD_BYTES + 1) + 2
MAX_LEGACY_STATE_BYTES = 256 * MAX_RECORD_BYTES + 1024 * 1024
LEGACY_INPUT_AGE = timedelta(minutes=15)
MAX_TARIFF_HANDOFF_DELAY = timedelta(seconds=1)
_QUOTE_FIELDS = (
    "start", "end", "price", "source", "uncertainty_dkk_per_kwh", "tariff_start",
)


def timestamp(value: Any) -> datetime:
    """Parse one aware timestamp, normalizing DST folds to UTC."""
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Replay timestamps must be timezone-aware")
    return result.astimezone(UTC)


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError("Replay quantities must be finite numbers")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("Replay quantities must be finite numbers") from exc
    if not isfinite(number):
        raise ValueError("Replay quantities must be finite numbers")
    return number


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


def _actual_row(row: dict[str, Any]) -> tuple[datetime, datetime, dict[str, Any]]:
    start, end = timestamp(row["start"]), timestamp(row["end"])
    seconds = (end - start).total_seconds()
    load, covered, price = (_finite(row[key]) for key in ("load_wh", "covered_seconds", "price"))
    scope = row.get("scope")
    if (not 0 < seconds <= 3600 or load < 0 or not .9 * seconds <= covered <= seconds
            or not -20 <= price <= 100 or row.get("quality") != "good"
            or scope is not None and (not isinstance(scope, str) or len(scope) > 256)):
        raise ValueError("Invalid replay actual")
    return start, end, {"start": start, "end": end, "load_wh": load,
                        "covered_seconds": covered, "price": price, "quality": "good", "scope": scope}


def _share_quotes(rows: list[bytes]) -> bytes:
    """Store overlapping market quotes once per page, retaining each issued load."""
    quotes: dict[tuple, int] = {}
    records = []
    for encoded in rows:
        record = json.loads(encoded)
        slots = []
        for slot in record["slots"]:
            quote = tuple(slot.get(key) for key in _QUOTE_FIELDS)
            slots.append((quotes.setdefault(quote, len(quotes)), slot["expected_load_wh"]))
        records.append({**record, "slots": slots})
    return _json({"quotes": list(quotes), "records": records}).encode()


def _decode_page(encoded: Any) -> list[dict[str, Any]]:
    data = _decode(encoded, MAX_STATE_BYTES)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise TypeError("Invalid replay page")
    quotes = [dict(zip(_QUOTE_FIELDS, quote, strict=True)) for quote in data["quotes"]]
    records = data["records"]
    if not isinstance(records, list) or len(records) > _PLAN_PAGE_SIZE:
        raise ValueError("Invalid replay plan page")
    for record in records:
        slots = record["slots"]
        if not isinstance(slots, list) or not 0 < len(slots) <= MAX_SLOTS:
            raise ValueError("Invalid replay plan page horizon")
        expanded = []
        for index, load in slots:
            if type(index) is not int or not 0 <= index < len(quotes):
                raise ValueError("Invalid replay quote index")
            expanded.append({**quotes[index], "expected_load_wh": load})
        record["slots"] = expanded
    return records


@dataclass(slots=True)
class _Page:
    """One ordered group of canonical rows with a reusable compressed image."""

    times: list[datetime] = field(default_factory=list)
    rows: list[bytes] | None = field(default_factory=list)
    encoded: str | None = None

    def decoded(self) -> list[dict[str, Any]]:
        if self.rows is not None:
            return [json.loads(row) for row in self.rows]
        assert self.encoded is not None
        return _decode_page(self.encoded)

    def last_row(self) -> dict[str, Any]:
        return json.loads(self.rows[-1]) if self.rows is not None else self.decoded()[-1]

    def writable(self) -> list[bytes]:
        if self.rows is None:
            self.rows = [_json(row).encode() for row in self.decoded()]
        self.encoded = None
        return self.rows

    def pack(self, *, sealed: bool, share_quotes: bool) -> str:
        if self.encoded is None:
            assert self.rows is not None
            payload = _share_quotes(self.rows) if share_quotes else b"[" + b",".join(self.rows) + b"]"
            self.encoded = base64.b64encode(zlib.compress(payload, level=3)).decode("ascii")
        if sealed:
            self.rows = None
        return self.encoded


def _decode(encoded: Any, limit: int) -> Any:
    if not isinstance(encoded, str) or len(encoded) > (limit + 1024) * 2:
        raise ValueError("Oversized replay payload")
    decoder = zlib.decompressobj()
    payload = decoder.decompress(base64.b64decode(encoded, validate=True), limit + 1)
    if len(payload) > limit or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid replay payload")
    return json.loads(payload)


@dataclass(slots=True)
class _Archive:
    """Record index for age/count pruning without unpacking retained horizons."""

    page_size: int
    limit: int
    time_key: str
    pages: deque[_Page] = field(default_factory=deque)
    count: int = 0

    @property
    def first(self) -> datetime | None:
        return self.pages[0].times[0] if self.pages else None

    @property
    def last(self) -> datetime | None:
        return self.pages[-1].times[-1] if self.pages else None

    def append(self, at: datetime, row: bytes) -> None:
        if not self.pages or len(self.pages[-1].times) == self.page_size:
            self.pages.append(_Page())
        page = self.pages[-1]
        page.writable().append(row)
        page.times.append(at)
        self.count += 1
        self._drop_first(self.count - self.limit)

    def _drop_first(self, count: int) -> None:
        while count > 0:
            page = self.pages[0]
            remove = min(len(page.times), count)
            if remove == len(page.times):
                self.pages.popleft()
            else:
                del page.writable()[:remove]
                del page.times[:remove]
            count -= remove
            self.count -= remove

    def prune(self, cutoff: datetime, upper: datetime, *, inclusive_cutoff: bool) -> None:
        before = bisect_left if inclusive_cutoff else bisect_right
        while self.pages:
            times = self.pages[0].times
            remove = before(times, cutoff)
            removed_entire_page = remove == len(times)
            self._drop_first(remove)
            if not removed_entire_page:
                break
        while self.pages and self.pages[-1].times[-1] > upper:
            page = self.pages[-1]
            keep = bisect_right(page.times, upper)
            self.count -= len(page.times) - keep
            if keep:
                del page.writable()[keep:]
                del page.times[keep:]
            else:
                self.pages.pop()

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        for page in self.pages:
            yield from page.decoded()

    def pack(self) -> list[str]:
        return [page.pack(sealed=index < len(self.pages) - 1, share_quotes=self.time_key == "issued_at")
                for index, page in enumerate(self.pages)]

    def restore(self, chunks: Any) -> None:
        if not isinstance(chunks, list) or len(chunks) > self.limit // self.page_size + 2:
            return
        for encoded in chunks:
            try:
                rows = _decode_page(encoded)
                if not isinstance(rows, list) or not 0 < len(rows) <= self.page_size:
                    continue
                times = [timestamp(row[self.time_key]) for row in rows]
                if any(left >= right for left, right in pairwise(times)):
                    continue
                if self.last is not None and times[0] <= self.last:
                    continue
                if self.time_key == "issued_at":
                    for row, at in zip(rows, times, strict=True):
                        _validate_issued_metadata(row)
                        if not {"settings", "slots"} <= row.keys() or validity_end(row, at) < at:
                            raise ValueError("Invalid replay plan page")
                else:
                    previous_end = self.last
                    for row in rows:
                        start, end, _ = _actual_row(row)
                        if previous_end is not None and start < previous_end:
                            raise ValueError("Overlapping replay actuals")
                        previous_end = end
                self.pages.append(_Page(times, rows=None, encoded=encoded))
                self.count += len(times)
                self._drop_first(self.count - self.limit)
            except (ValueError, TypeError, KeyError, binascii.Error, zlib.error):
                continue


def validity_end(row: dict[str, Any], issued: datetime) -> datetime:
    """Legacy snapshots carry only the original 15-minute evidence window."""
    if "valid_until" not in row:
        return issued + LEGACY_INPUT_AGE
    return timestamp(row["valid_until"])


def _validate_issued_metadata(row: dict[str, Any]) -> None:
    scope = row["scope"]
    if (not isinstance(scope, str) or not scope or len(scope) > 256
            or not 0 <= _finite(row["soc"]) <= 100
            or _finite(row.get("terminal_price_floor_dkk_per_kwh", 0.0)) < 0
            or not isinstance(row.get("current_discharge_enabled", True), bool)
            or not isinstance(row.get("interrupted", False), bool)):
        raise ValueError("Invalid replay plan metadata")
    Action(row["current_action"])


@dataclass(slots=True)
class ReplayHistory:
    """Keep lossless, independently compressed groups of issued and actual rows."""

    _records: _Archive = field(
        default_factory=lambda: _Archive(_PLAN_PAGE_SIZE, MAX_SNAPSHOTS, "issued_at"), repr=False,
    )
    _actuals: _Archive = field(default_factory=lambda: _Archive(96, MAX_ACTUALS, "end"), repr=False)

    def record(
        self, issued_at: datetime, *, slots: Sequence[PriceSlot], settings: PlannerSettings,
        soc: float, scope: str, current_action: Action = Action.GRID,
        committed_slot: PlannedSlot | None = None,
        terminal_price_floor_dkk_per_kwh: float = 0.0,
        current_discharge_enabled: bool = True,
    ) -> bool:
        """Freeze the inputs and limit their validity to the current known tariff."""
        issued = timestamp(issued_at)
        deadline = next((timestamp(slot.end) for slot in slots if
                         slot.source == "known" and timestamp(slot.start) <= issued < timestamp(slot.end)),
                        issued)
        return self._record_issued({
            "issued_at": issued, "valid_until": deadline, "scope": scope, "soc": soc,
            "current_action": current_action, "settings": asdict(settings),
            "slots": [asdict(slot) for slot in slots],
            "committed_slot": committed_slot.as_dict() if committed_slot else None,
            "terminal_price_floor_dkk_per_kwh": terminal_price_floor_dkk_per_kwh,
            "current_discharge_enabled": current_discharge_enabled,
        })

    def _record_issued(self, row: dict[str, Any], *, restoring: bool = False) -> bool:
        try:
            issued = timestamp(row["issued_at"])
            if self._records.last is not None and issued <= self._records.last:
                return False
            _validate_issued_metadata(row)
            restore_settings(row["settings"])
            slots = restore_slots(row["slots"])
            restore_commitment(row.get("committed_slot"))
            if restoring:
                row = {**row, "slots": [asdict(slot) for slot in slots]}
            valid_until = validity_end(row, issued)
            if "valid_until" in row:
                known_end = next((slot.end for slot in slots
                                  if slot.source == "known" and slot.start <= issued < slot.end), issued)
                if not issued <= valid_until <= known_end:
                    return False
                row = {**row, "valid_until": valid_until}
            row = {**row, "issued_at": issued, "current_action": Action(row["current_action"])}
            serialized = _json(row).encode()
            if len(serialized) > MAX_RECORD_BYTES:
                return False
            if not restoring and self._records.pages:
                last = self._records.pages[-1].last_row()
                if issued < validity_end(last, timestamp(last["issued_at"])):
                    ignored = {"issued_at", "valid_until"}
                    content = json.loads(serialized)
                    if all(content.get(key) == last.get(key) for key in content.keys() - ignored):
                        return False
            self.prune(issued)
            self._records.append(issued, serialized)
            return True
        except (TypeError, ValueError, KeyError):
            return False

    def close(self, now: datetime, *, interrupted: bool = True) -> bool:
        """End a vintage, retaining interruptions at a normal tariff deadline."""
        if not self._records.pages:
            return False
        now = timestamp(now)
        page = self._records.pages[-1]
        issued = page.times[-1]
        if now < issued:
            return False
        row = page.last_row()
        if "valid_until" not in row:
            return False
        expires = validity_end(row, issued)
        if now >= expires and (
            not interrupted or row.get("interrupted") or now - expires > MAX_TARIFF_HANDOFF_DELAY
        ):
            return False
        if interrupted:
            row["interrupted"] = True
        page.writable()[-1] = _json({**row, "valid_until": min(now, expires)}).encode()
        return True

    def record_actual(self, row: dict[str, Any]) -> bool:
        """Archive completed measured intervals without inventing coverage."""
        try:
            start, end, data = _actual_row(row)
            if self._actuals.last is not None and start < self._actuals.last:
                return False
            self._actuals.append(end, _json(data).encode())
            self._actuals.prune(end - RETENTION, end, inclusive_cutoff=False)
            return True
        except (TypeError, ValueError, KeyError):
            return False

    def prune(self, now: datetime) -> None:
        """Enforce age limits using the timestamp indexes."""
        now = timestamp(now)
        self._records.prune(now - RETENTION, now, inclusive_cutoff=True)
        self._actuals.prune(now - RETENTION, now, inclusive_cutoff=False)

    def iter_records(self) -> Iterator[dict[str, Any]]:
        return self._records.iter_rows()

    def iter_actuals(self) -> Iterator[dict[str, Any]]:
        return self._actuals.iter_rows()

    def as_dict(self) -> dict[str, Any]:
        """Expand the stable export shape independently of the storage layout."""
        return {"schema_version": SCHEMA_VERSION, "records": list(self.iter_records()),
                "actuals": list(self.iter_actuals())}

    def as_storage_dict(self) -> dict[str, Any]:
        """Reuse compressed groups while packing only new or changed rows."""
        return {"schema_version": SCHEMA_VERSION, "encoding": CHUNK_ENCODING,
                "records": self._records.pack(), "actuals": self._actuals.pack()}

    @classmethod
    def from_dict(cls, data: Any) -> ReplayHistory:
        history = cls()
        if not isinstance(data, dict) or data.get("schema_version") not in (1, SCHEMA_VERSION):
            return history
        encoding = data.get("encoding")
        if encoding == CHUNK_ENCODING and data["schema_version"] == SCHEMA_VERSION:
            history._records.restore(data.get("records"))
            history._actuals.restore(data.get("actuals"))
            return history
        if encoding == "zlib-base64" and data["schema_version"] == 1:
            try:
                data = _decode(data.get("payload"), MAX_LEGACY_STATE_BYTES)
                if not isinstance(data, dict) or data.get("schema_version") != 1 or "encoding" in data:
                    return history
            except (ValueError, TypeError, binascii.Error, zlib.error):
                return history
        elif "encoding" in data:
            return history
        records, actuals = data.get("records", []), data.get("actuals", [])
        for row in records[-MAX_SNAPSHOTS:] if isinstance(records, list) else []:
            if isinstance(row, dict):
                history._record_issued(row, restoring=True)
        for row in actuals[-MAX_ACTUALS:] if isinstance(actuals, list) else []:
            if isinstance(row, dict):
                history.record_actual(row)
        return history

    def export(self, *, compact: bool = False) -> dict[str, Any]:
        evidence = self.as_storage_dict() if compact else self.as_dict()
        return {**evidence, **self.summary()}

    def summary(self) -> dict[str, Any]:
        """Expose retained evidence bounds without expanding stored horizons."""
        return {"snapshot_count": self._records.count, "actual_count": self._actuals.count,
                "first_issued_at": self._records.first.isoformat() if self._records.first else None,
                "last_issued_at": self._records.last.isoformat() if self._records.last else None,
                "first_actual_start": self._actuals.pages[0].decoded()[0]["start"]
                if self._actuals.pages else None,
                "last_actual_end": self._actuals.last.isoformat() if self._actuals.last else None,
                "max_snapshots": MAX_SNAPSHOTS, "retention_days": RETENTION.days,
                "method": "issued_inputs_with_separate_realized_interval_means"}
