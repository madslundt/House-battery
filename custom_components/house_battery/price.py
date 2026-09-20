"""Normalize common Home Assistant electricity-price entity formats."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any

from .models import PriceSlot


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
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
    if start is None:
        return None
    if (end is not None and end <= start) or price < -20 or price > 100:
        return None
    return start, end, price


def normalize_price_rows(rows: Iterable[Any]) -> list[PriceSlot]:
    """Return sorted source intervals, inferring missing ends from their cadence.

    Price providers need not share a cadence: an hourly known-price feed and a
    quarter-hour forecast feed remain hourly and quarter-hourly respectively.
    The final row without an ``end`` uses the source's median start-to-start
    interval; only a single isolated row falls back to one hour.
    """
    parsed_rows = [parsed for value in rows if (parsed := _row(value)) is not None]
    parsed_rows = [
        row
        for row in parsed_rows
        if row[0].tzinfo is not None and (row[1] is None or row[1].tzinfo is not None)
    ]
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
    return sorted(normalized.values(), key=lambda item: item.start)


def extract_rows(attributes: dict[str, Any]) -> list[Any]:
    """Extract only documented/common list attributes from a price entity."""
    rows: list[Any] = []
    for key in ("prices", "raw_today", "raw_tomorrow", "today", "tomorrow"):
        value = attributes.get(key)
        if isinstance(value, list):
            rows.extend(value)
    return rows
