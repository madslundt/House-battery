"""Persist the measured battery activity timeline for recent local days."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any


@dataclass(slots=True)
class ActualDailyHistory:
    """Coalesce measured samples into durable daily activity blocks."""

    days: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    last_sample_at: str | None = None
    sample_count: int = 0
    _sample_continuity: bool = field(default=False, init=False, repr=False, compare=False)

    def prune(self, now: datetime) -> None:
        """Retain eight local calendar days, including after offline gaps."""
        first_day = now.date() - timedelta(days=7)
        retained = {}
        for key, blocks in self.days.items():
            try:
                if first_day <= date.fromisoformat(key) <= now.date():
                    retained[key] = blocks
            except (TypeError, ValueError):
                continue
        self.days = dict(sorted(retained.items()))
        if self.last_sample_at:
            try:
                timestamp = datetime.fromisoformat(self.last_sample_at)
                if (
                    timestamp.tzinfo is None
                    or not first_day <= timestamp.date() <= now.date()
                ):
                    self.last_sample_at = None
            except (TypeError, ValueError):
                self.last_sample_at = None

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
        self.prune(now)
        day = now.date().isoformat()
        had_day = day in self.days
        prior_timestamp = (
            datetime.fromisoformat(self.last_sample_at) if self.last_sample_at else None
        )
        gap = (
            max(0.0, (now.astimezone(UTC) - prior_timestamp.astimezone(UTC)).total_seconds())
            if prior_timestamp is not None else 0.0
        )
        offline_gap = prior_timestamp is not None and gap > 0 and (
            not self._sample_continuity or gap > 120
        )
        elapsed = gap if (
            prior_timestamp is not None and prior_timestamp.date() == now.date()
            and not offline_gap
        ) else 0.0

        charge = _nonnegative(charge_w)
        discharge = _nonnegative(discharge_w)
        soc = _optional_number(soc)
        price = _optional_number(price)
        action = _activity(charge, discharge)
        blocks = self.days.setdefault(day, [])
        block = blocks[-1] if blocks else None
        if offline_gap:
            if block is None or block["action"] != "unknown":
                gap_start = max(
                    prior_timestamp, now.replace(hour=0, minute=0, second=0, microsecond=0)
                )
                block = _new_block(gap_start.isoformat(), "unknown", None)
                blocks.append(block)
            block["end"] = timestamp
        if action == "unknown" or (block is not None and block["action"] == "unknown"):
            # A recovered sample cannot describe energy during the preceding
            # unknown interval. Resume integration with the next fresh sample.
            elapsed = 0.0
        if block is None or block["action"] != action:
            if block is not None:
                block["end"] = (
                    timestamp if block["action"] == "unknown"
                    else prior_timestamp.isoformat() if prior_timestamp else timestamp
                )
            block = _new_block(timestamp, action, soc)
            blocks.append(block)

        hours = elapsed / 3600
        # Missing telemetry is recorded as an ``unknown`` activity block. Do
        # not fail the coordinator refresh (and leave every HA entity stale)
        # when a local TCP sample is unavailable; account only measured energy.
        charge_kwh = charge * hours / 1000 if charge is not None else 0.0
        discharge_kwh = discharge * hours / 1000 if discharge is not None else 0.0
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
        self._sample_continuity = True
        # Keep one week of compact, coalesced daily blocks for review.
        self.days = dict(sorted(self.days.items())[-8:])
        activity_changed = block["samples"] == 1
        return not had_day or activity_changed or now.minute % 5 == 0

    def today(self, now: datetime) -> list[dict[str, Any]]:
        """Return a detached view of today's measured blocks."""
        return [dict(block) for block in self.days.get(now.date().isoformat(), [])]

    def round_trip_summary(self, now: datetime) -> dict[str, Any]:
        """Estimate AC-side round-trip efficiency over a SOC-balanced window.

        The estimate uses measured charge and discharge telemetry from the last
        seven calendar days. It is only reported when the window starts and
        ends at nearly the same SOC, so a net change in stored energy does not
        dominate the energy ratio.
        """
        first_day = (now.date() - timedelta(days=6)).isoformat()
        recent_days = [
            (day, blocks)
            for day, blocks in sorted(self.days.items())
            if first_day <= day <= now.date().isoformat() and blocks
        ]
        blocks = [block for _, day_blocks in recent_days for block in day_blocks]
        charge_kwh = sum(_nonnegative(block.get("charge_kwh")) or 0 for block in blocks)
        discharge_kwh = sum(
            _nonnegative(block.get("discharge_kwh")) or 0 for block in blocks
        )
        start_soc = _optional_number(blocks[0].get("soc_start")) if blocks else None
        end_soc = _optional_number(blocks[-1].get("soc_end")) if blocks else None
        soc_change = (
            end_soc - start_soc
            if start_soc is not None and end_soc is not None
            else None
        )
        efficiency = None
        status = "insufficient_energy"
        if not blocks:
            status = "no_recent_measurements"
        elif charge_kwh <= 0 or discharge_kwh <= 0:
            status = "insufficient_energy"
        elif soc_change is None:
            status = "soc_unavailable"
        elif abs(soc_change) > 5:
            status = "soc_window_unbalanced"
        else:
            candidate = discharge_kwh / charge_kwh
            if 0.5 <= candidate <= 1.0:
                efficiency = candidate * 100
                status = "ready"
            else:
                status = "outside_expected_range"
        return {
            "efficiency_pct": efficiency,
            "charge_kwh": round(charge_kwh, 3),
            "discharge_kwh": round(discharge_kwh, 3),
            "soc_start": start_soc,
            "soc_end": end_soc,
            "soc_change_pct_points": (
                round(soc_change, 2) if soc_change is not None else None
            ),
            "window_start": recent_days[0][0] if recent_days else None,
            "window_end": recent_days[-1][0] if recent_days else None,
            "status": status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActualDailyHistory:
        """Restore a saved history while discarding malformed entries."""
        days: dict[str, list[dict[str, Any]]] = {}
        for day, blocks in data.get("days", {}).items():
            if not isinstance(day, str) or not isinstance(blocks, list):
                continue
            days[day] = [_restore_block(block) for block in blocks if isinstance(block, dict)]
        return cls(
            days=dict(sorted(days.items())[-8:]),
            last_sample_at=(
                data.get("last_sample_at")
                if isinstance(data.get("last_sample_at"), str)
                else None
            ),
            sample_count=int(_nonnegative(data.get("sample_count")) or 0),
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


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _restore_block(value: dict[str, Any]) -> dict[str, Any]:
    """Repair old JSON nulls without inventing priced energy evidence."""
    block = dict(value)
    for key in ("charge_kwh", "discharge_kwh", "priced_charge_kwh", "priced_discharge_kwh"):
        block[key] = _nonnegative(block.get(key)) or 0.0
    for cost_key, energy_key in (
        ("charge_cost_dkk", "priced_charge_kwh"),
        ("discharge_value_dkk", "priced_discharge_kwh"),
    ):
        cost = _optional_number(block.get(cost_key))
        block[cost_key] = cost if cost is not None else 0.0
        if cost is None:
            block[energy_key] = 0.0
    for key in ("soc_start", "soc_end", "soc_min", "soc_max"):
        block[key] = _optional_number(block.get(key))
    block["samples"] = int(_nonnegative(block.get("samples")) or 0)
    block.setdefault("action", "unknown")
    return block


def _new_block(timestamp: str, action: str, soc: float | None) -> dict[str, Any]:
    return {
        "start": timestamp, "end": timestamp, "action": action,
        "soc_start": soc, "soc_end": soc, "soc_min": soc, "soc_max": soc,
        "charge_kwh": 0.0, "discharge_kwh": 0.0,
        "charge_cost_dkk": 0.0, "discharge_value_dkk": 0.0,
        "priced_charge_kwh": 0.0, "priced_discharge_kwh": 0.0,
        "samples": 0,
    }
