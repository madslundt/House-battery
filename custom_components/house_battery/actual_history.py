"""Persist the measured battery activity timeline for recent local days."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any


@dataclass(slots=True)
class ActualDailyHistory:
    """Coalesce measured samples into durable daily activity blocks."""

    days: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    last_sample_at: str | None = None
    sample_count: int = 0

    def record(
        self,
        now: datetime,
        soc: float | None,
        charge_w: float | None,
        discharge_w: float | None,
        price: float | None,
    ) -> bool:
        """Record measured state; return whether this sample should be persisted."""
        if now.tzinfo is None:
            raise ValueError("Actual battery history requires an aware timestamp")
        timestamp = now.isoformat()
        day = now.date().isoformat()
        had_day = day in self.days
        prior_timestamp = (
            datetime.fromisoformat(self.last_sample_at)
            if self.last_sample_at
            else None
        )
        elapsed = 0.0
        if prior_timestamp is not None and prior_timestamp.date() == now.date():
            elapsed = min(
                120.0,
                max(
                    0.0,
                    (
                        now.astimezone(UTC) - prior_timestamp.astimezone(UTC)
                    ).total_seconds(),
                ),
            )

        charge = _nonnegative(charge_w)
        discharge = _nonnegative(discharge_w)
        action = _activity(charge, discharge)
        blocks = self.days.setdefault(day, [])
        block = blocks[-1] if blocks else None
        if block is None or block["action"] != action:
            if block is not None:
                block["end"] = (
                    prior_timestamp.isoformat() if prior_timestamp else timestamp
                )
            block = {
                "start": timestamp,
                "end": timestamp,
                "action": action,
                "soc_start": soc,
                "soc_end": soc,
                "soc_min": soc,
                "soc_max": soc,
                "charge_kwh": 0.0,
                "discharge_kwh": 0.0,
                "charge_cost_dkk": 0.0,
                "discharge_value_dkk": 0.0,
                "priced_charge_kwh": 0.0,
                "priced_discharge_kwh": 0.0,
                "samples": 0,
            }
            blocks.append(block)

        hours = elapsed / 3600
        charge_kwh = charge * hours / 1000
        discharge_kwh = discharge * hours / 1000
        block["charge_kwh"] += charge_kwh
        block["discharge_kwh"] += discharge_kwh
        if price is not None:
            block["charge_cost_dkk"] += charge_kwh * price
            block["discharge_value_dkk"] += discharge_kwh * price
            block["priced_charge_kwh"] += charge_kwh
            block["priced_discharge_kwh"] += discharge_kwh
        block["end"] = timestamp
        block["samples"] += 1
        if soc is not None:
            block["soc_end"] = soc
            block["soc_min"] = (
                soc if block["soc_min"] is None else min(block["soc_min"], soc)
            )
            block["soc_max"] = (
                soc if block["soc_max"] is None else max(block["soc_max"], soc)
            )

        self.sample_count += 1
        self.last_sample_at = timestamp
        # Keep one week of compact, coalesced daily blocks for review.
        self.days = dict(sorted(self.days.items())[-8:])
        activity_changed = block["samples"] == 1
        return not had_day or activity_changed or now.minute % 5 == 0

    def today(self, now: datetime) -> list[dict[str, Any]]:
        """Return a detached view of today's measured blocks."""
        return [dict(block) for block in self.days.get(now.date().isoformat(), [])]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActualDailyHistory:
        """Restore a saved history while discarding malformed entries."""
        days: dict[str, list[dict[str, Any]]] = {}
        for day, blocks in data.get("days", {}).items():
            if not isinstance(day, str) or not isinstance(blocks, list):
                continue
            days[day] = [dict(block) for block in blocks if isinstance(block, dict)]
        return cls(
            days=dict(sorted(days.items())[-8:]),
            last_sample_at=(
                data.get("last_sample_at")
                if isinstance(data.get("last_sample_at"), str)
                else None
            ),
            sample_count=max(0, int(data.get("sample_count", 0))),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "last_sample_at": self.last_sample_at,
            "sample_count": self.sample_count,
        }


def _nonnegative(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, number) if isfinite(number) else None


def _activity(charge_w: float | None, discharge_w: float | None) -> str:
    if charge_w is None or discharge_w is None:
        return "unknown"
    if charge_w >= 10 and discharge_w >= 10:
        return "conflict"
    if charge_w >= 10:
        return "charging"
    if discharge_w >= 10:
        return "discharging"
    return "idle"
