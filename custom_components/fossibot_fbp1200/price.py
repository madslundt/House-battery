"""Normalize common Home Assistant electricity-price entity formats."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
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


def _row(row: Any) -> tuple[datetime, datetime, float] | None:
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
    if end is None:
        end = start + timedelta(hours=1)
    if end <= start or price < -20 or price > 100:
        return None
    return start, end, price


def normalize_price_rows(rows: Iterable[Any]) -> list[PriceSlot]:
    """Return sorted 15-minute intervals without inventing missing data."""
    normalized: dict[tuple[datetime, datetime], PriceSlot] = {}
    for value in rows:
        parsed = _row(value)
        if parsed is None:
            continue
        start, end, price = parsed
        if start.tzinfo is None or end.tzinfo is None:
            continue
        cursor = start
        while cursor < end:
            interval_end = min(end, cursor + timedelta(minutes=15))
            normalized[(cursor, interval_end)] = PriceSlot(cursor, interval_end, price)
            cursor = interval_end
    return sorted(normalized.values(), key=lambda item: item.start)


def extract_rows(attributes: dict[str, Any]) -> list[Any]:
    """Extract only documented/common list attributes from a price entity."""
    rows: list[Any] = []
    for key in ("prices", "raw_today", "raw_tomorrow", "today", "tomorrow"):
        value = attributes.get(key)
        if isinstance(value, list):
            rows.extend(value)
    return rows
