"""Normalize common Home Assistant electricity-price entity formats."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite
from statistics import median
from typing import Any

from .models import PriceSlot


@dataclass(frozen=True, slots=True)
class PriceNormalization:
    """Normalized slots together with enough evidence to reject bad inputs."""

    slots: tuple[PriceSlot, ...]
    source_rows: int
    invalid_rows: int


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        # Native provider timestamps can share a ZoneInfo object across a
        # repeated DST hour. Python then compares/subtracts wall times, which
        # collapses the two folds and mistakes 02:45 -> 02:00 for midnight.
        # Normalize aware datetimes before deduplication and interval math.
        return value.astimezone(UTC) if value.tzinfo is not None else value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row(row: Any) -> tuple[datetime, datetime | None, float] | None:
    if not isinstance(row, dict):
        return None
    start = _datetime(
        row.get("start") or row.get("hour") or row.get("time") or row.get("Time")
    )
    end = _datetime(row.get("end"))
    raw_price = row.get("price", row.get("value", row.get("Price")))
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return None
    if (
        start is None or start.tzinfo is None or not isfinite(price)
        or (end is not None and end.tzinfo is None)
        or (row.get("end") is not None and end is None)
    ):
        return None
    # Handle midnight rollover: intervals like 23:45→00:00 have end < start
    # because the end is on the next calendar day.  Add one day when the raw
    # end precedes the start, which is the standard way price feeds express
    # overnight intervals (e.g. 23:45-00:00).
    if end is not None and end < start:
        end = end + timedelta(days=1)
    if (end is not None and end <= start) or price < -20 or price > 100:
        return None
    return start, end, price


def normalize_price_rows(rows: Iterable[Any]) -> list[PriceSlot]:
    """Return the valid source intervals without exposing input diagnostics."""
    return list(normalize_price_rows_with_report(rows).slots)


def normalize_price_rows_with_report(rows: Iterable[Any]) -> PriceNormalization:
    """Return sorted source intervals, inferring missing ends from their cadence.

    Price providers need not share a cadence: an hourly known-price feed and a
    quarter-hour forecast feed remain hourly and quarter-hourly respectively.
    The final row without an ``end`` uses the source's median start-to-start
    interval; only a single isolated row falls back to one hour.
    """
    source_rows = 0
    invalid_rows = 0
    parsed_rows: list[tuple[datetime, datetime | None, float]] = []
    for value in rows:
        source_rows += 1
        parsed = _row(value)
        if (
            parsed is None
            or parsed[0].tzinfo is None
            or (parsed[1] is not None and parsed[1].tzinfo is None)
        ):
            invalid_rows += 1
            continue
        parsed_rows.append(parsed)
    starts = sorted({start for start, _, _ in parsed_rows})
    gaps = [
        (later - earlier).total_seconds()
        for earlier, later in pairwise(starts)
        if later > earlier
    ]
    fallback = timedelta(seconds=median(gaps)) if gaps else timedelta(hours=1)
    next_start = {
        start: starts[index + 1] if index + 1 < len(starts) else None
        for index, start in enumerate(starts)
    }
    normalized: dict[tuple[datetime, datetime], PriceSlot] = {}
    for start, end, price in parsed_rows:
        interval_end = end or next_start[start] or start + fallback
        if interval_end <= start:
            continue
        normalized[(start, interval_end)] = PriceSlot(start, interval_end, price)
    return PriceNormalization(
        tuple(sorted(normalized.values(), key=lambda item: item.start)),
        source_rows=source_rows,
        invalid_rows=invalid_rows,
    )


def extract_rows(attributes: dict[str, Any]) -> list[Any]:
    """Extract only documented/common list attributes from a price entity."""
    rows: list[Any] = []
    for key in ("prices", "raw_today", "raw_tomorrow", "today", "tomorrow"):
        value = attributes.get(key)
        if isinstance(value, list):
            rows.extend(value)
    return rows


def is_forecast_data(attributes: dict[str, Any]) -> bool:
    """Interpret the provider's ``forecast_data`` flag without string traps."""
    value = attributes.get("forecast_data")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return isinstance(value, str) and value.strip().casefold() in {
        "true",
        "yes",
        "on",
        "1",
    }


def diagnose_price_sources(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Describe explicit unit contradictions and duplicate conflicts, without conversion.

    Entity metadata cannot establish which taxes/network components its interval
    rows include. The supplied all-in DKK/kWh contract remains the source owner's
    responsibility; absent metadata is unverified rather than an error.
    """
    reports: dict[str, dict[str, Any]] = {}
    intervals: dict[tuple[datetime, datetime], list[tuple[str, float]]] = {}
    for source, attributes in sources.items():
        unit = str(attributes.get("unit_of_measurement", "")).strip()
        currency = str(attributes.get("currency", "")).strip().upper()
        normalized = unit.casefold().replace(" ", "")
        if normalized == "dkk/kwh" or normalized == "kr/kwh" and currency == "DKK":
            status = "compatible"
        elif normalized in {"", "kr/kwh"} and currency in {"", "DKK"}:
            status = "unverified"
        else:
            status = "incompatible"
        if currency and currency != "DKK":
            status = "incompatible"
        reports[source] = {
            "reported_unit": unit or None, "reported_currency": currency or None,
            "unit_status": status, "required_basis": "all_in_marginal_import_dkk_per_kwh",
            "component_completeness": "supplied_by_source_not_inferred",
        }
        for slot in normalize_price_rows(extract_rows(attributes)):
            intervals.setdefault((slot.start, slot.end), []).append((source, slot.price))
    conflicts = [
        {"start": start.isoformat(), "end": end.isoformat(),
         "sources": [{"entity_id": source, "price": price} for source, price in prices]}
        for (start, end), prices in intervals.items()
        if max(price for _, price in prices) - min(price for _, price in prices) > 1e-9
    ]
    return {"sources": reports, "conflicting_intervals": conflicts,
            "conflict_count": len(conflicts)}
