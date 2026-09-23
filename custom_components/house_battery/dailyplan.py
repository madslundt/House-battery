"""Persisted, reconciled daily optimization timeline.

The optimizer (:mod:`planner`) only ever plans the *future*: from ``now`` to the
end of the known/forecast price horizon.  The dashboard, however, must always
show the complete current local day (``00:00 -> 24:00``).  This module bridges
the two by keeping a small, timezone-aware, per-day timeline that

* is persisted so it survives restarts/reloads and midnight,
* never rewrites the already-published past (intervals before the replan
  ``cutoff`` are immutable),
* replaces only the future portion on every replan, and
* stays invariant-clean (sorted, non-overlapping, non-duplicate, non-zero
  duration, ``start < end``, bounded to the local calendar day).

The stored timeline keeps one slot per price interval so the economic detail
(per-slot cost/energy/SOC) stays correct.  Adjacent identical-action slots are
merged lazily for display via :func:`merge_adjacent_blocks`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .models import Action, PlannedSlot


def local_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` of the local calendar day containing ``now``.

    ``now`` must be timezone-aware; ``.date()`` yields the *local* calendar
    date, so a plan created at 00:19 local (UTC+2) belongs to that local day.
    Midnight and the next midnight are expressed in ``now``'s own offset so DST
    transitions keep the day at a single contiguous span.
    """
    local_date = now.date()
    start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=now.tzinfo)
    end = start + timedelta(days=1)
    return start, end


def _clip_to_day(slot: PlannedSlot, start: datetime, end: datetime) -> PlannedSlot | None:
    """Clip a slot to ``[start, end)``; drop it entirely if it does not fit."""
    new_start = max(slot.start, start)
    new_end = min(slot.end, end)
    if new_start >= new_end:
        return None
    return replace(slot, start=new_start, end=new_end)


def _history_before(
    slots: tuple[PlannedSlot, ...], cutoff: datetime, day_start: datetime, day_end: datetime
) -> list[PlannedSlot]:
    """Keep the published past, truncating any interval that straddles ``cutoff``.

    Intervals ending at or before ``cutoff`` are kept whole.  Intervals starting
    at or after ``cutoff`` are dropped (they belong to the mutable future).  An
    interval that spans ``cutoff`` is truncated to ``[start, cutoff)`` so the
    timeline never overlaps or duplicates across the replan point.
    """
    kept: list[PlannedSlot] = []
    for slot in slots:
        if slot.end <= cutoff:
            clipped = _clip_to_day(slot, day_start, day_end)
            if clipped is not None:
                kept.append(clipped)
        elif slot.start >= cutoff:
            continue
        else:
            clipped = _clip_to_day(replace(slot, end=cutoff), day_start, day_end)
            if clipped is not None:
                kept.append(clipped)
    return kept


def _future_clipped(
    slots: tuple[PlannedSlot, ...], cutoff: datetime, day_start: datetime, day_end: datetime
) -> list[PlannedSlot]:
    """Keep only the optimizer's future intervals, clipped to the day."""
    clipped: list[PlannedSlot] = []
    for slot in slots:
        # The optimizer starts at ``now``; anything at/after ``cutoff`` is the
        # mutable future.  Intervals before the cutoff are impossible here and
        # are ignored defensively.
        if slot.start < cutoff:
            continue
        kept = _clip_to_day(slot, day_start, day_end)
        if kept is not None:
            clipped.append(kept)
    return clipped


def _normalize(intervals: list[PlannedSlot]) -> list[PlannedSlot]:
    """Enforce the hard timeline invariants.

    Sorted chronologically, zero-duration/negative intervals removed, and any
    accidental overlap clipped so each point in time belongs to at most one
    interval.  The reconciler above constructs non-overlapping input, so this
    is defensive and keeps repeated replans from ever accumulating gaps,
    duplicates or overlaps.
    """
    ordered = sorted(intervals, key=lambda item: (item.start, item.end))
    result: list[PlannedSlot] = []
    for slot in ordered:
        if slot.start >= slot.end:
            continue
        if result and slot.start < result[-1].end:
            # Overlap after reconciliation: clip away the already-covered head so
            # the two intervals remain disjoint.  Fully covered intervals drop.
            if slot.start <= result[-1].end:
                head = replace(slot, start=result[-1].end)
                if head.start >= head.end:
                    continue
                result.append(head)
            continue
        result.append(slot)
    return result


def reconcile_daily_plan(
    existing: "DailyPlan | None",
    future_slots: tuple[PlannedSlot, ...],
    *,
    cutoff: datetime,
    day_start: datetime,
    day_end: datetime,
) -> DailyPlan:
    """Rebuild the daily timeline at ``cutoff`` from history + a fresh plan.

    ``existing`` is the previously persisted timeline for the current local
    day.  Its past (before ``cutoff``) is preserved exactly; its future is
    discarded and replaced by ``future_slots`` (the optimizer's result).  A
    boundary replan (``cutoff`` landing on an interval edge) yields no duplicate
    or zero-duration interval.  If the day has rolled over, the previous day's
    timeline is never merged into the new one.
    """
    today = day_start.date().isoformat()
    if existing is None or existing.date != today:
        history: list[PlannedSlot] = []
    else:
        history = _history_before(existing.slots, cutoff, day_start, day_end)
    future = _future_clipped(future_slots, cutoff, day_start, day_end)
    normalized = _normalize(history + future)
    return DailyPlan(date=today, slots=tuple(normalized), created_at=cutoff)


def merge_adjacent_blocks(slots: tuple[PlannedSlot, ...]) -> list[PlannedSlot]:
    """Merge contiguous slots that share the same effective action.

    Two intervals are merged only when they touch (``a.end == b.start``) and
    carry the same action; differing power/price within an action still merges
    because the *effective operating decision* is identical, which is what the
    dashboard renders as a single block.  Economic detail is summed so the
    merged block stays faithful to the underlying per-slot plan.
    """
    ordered = sorted((slot for slot in slots if slot.start < slot.end), key=lambda s: s.start)
    blocks: list[PlannedSlot] = []
    for slot in ordered:
        if (
            blocks
            and slot.start == blocks[-1].end
            and blocks[-1].action is slot.action
        ):
            previous = blocks[-1]
            blocks[-1] = PlannedSlot(
                start=previous.start,
                end=slot.end,
                action=previous.action,
                price=previous.price,
                expected_load_wh=previous.expected_load_wh + slot.expected_load_wh,
                grid_import_wh=previous.grid_import_wh + slot.grid_import_wh,
                battery_charge_wh=previous.battery_charge_wh + slot.battery_charge_wh,
                battery_discharge_wh=previous.battery_discharge_wh + slot.battery_discharge_wh,
                soc_start=previous.soc_start,
                soc_end=slot.soc_end,
                interval_cost_dkk=previous.interval_cost_dkk + slot.interval_cost_dkk,
                baseline_cost_dkk=previous.baseline_cost_dkk + slot.baseline_cost_dkk,
                reason=previous.reason,
                price_source=slot.price_source,
                price_uncertainty_dkk_per_kwh=slot.price_uncertainty_dkk_per_kwh,
            )
        else:
            blocks.append(slot)
    return blocks


def _slot_totals(slots: tuple[PlannedSlot, ...]) -> dict[str, float]:
    expected_cost = sum(slot.interval_cost_dkk for slot in slots)
    baseline_cost = sum(slot.baseline_cost_dkk for slot in slots)
    throughput = sum(
        slot.battery_charge_wh + slot.battery_discharge_wh for slot in slots
    ) / 1000
    return {
        "expected_cost_dkk": round(expected_cost, 4),
        "baseline_cost_dkk": round(baseline_cost, 4),
        "expected_savings_dkk": round(baseline_cost - expected_cost, 4),
        "battery_throughput_kwh": round(throughput, 4),
    }


@dataclass(frozen=True, slots=True)
class DailyPlan:
    """Immutable, normalized timeline for a single local calendar day."""

    date: str
    slots: tuple[PlannedSlot, ...] = ()
    created_at: datetime | None = None

    def view_dict(
        self,
        *,
        actual_soc: float | None = None,
        terminal_price_dkk_per_kwh: float | None = None,
    ) -> dict[str, Any]:
        """Return the dashboard-ready representation of the daily timeline.

        The blocks merge adjacent identical actions so the timeline reads as a
        sequence of operating periods rather than raw price intervals, and the
        observed SOC is surfaced alongside the planned curve so plan deviation
        is visible.
        """
        merged = merge_adjacent_blocks(self.slots)
        totals = _slot_totals(self.slots)
        blocks = [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "action": slot.action.value,
                "soc_start": round(float(slot.soc_start), 3),
                "soc_end": round(float(slot.soc_end), 3),
                "expected_cost_dkk": round(float(slot.interval_cost_dkk), 3),
                "expected_savings_dkk": round(
                    float(slot.baseline_cost_dkk - slot.interval_cost_dkk), 3
                ),
                "energy_kwh": round(
                    float(slot.battery_charge_wh + slot.battery_discharge_wh) / 1000, 3
                ),
                "expected_load_kwh": round(float(slot.expected_load_wh) / 1000, 3),
                "expected_grid_import_kwh": round(float(slot.grid_import_wh) / 1000, 3),
                "reason": slot.reason,
            }
            for slot in merged
        ]
        # Surface the observed SOC on the first block so the dashboard can
        # overlay the real curve against the planned curve (requirement #9)
        # without ever altering the published block data.
        if blocks and actual_soc is not None:
            blocks[0]["actual_soc"] = float(actual_soc)
        return {
            "date": self.date,
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            "actual_soc": float(actual_soc) if actual_soc is not None else None,
            "blocks": blocks,
            "horizon_slots": len(self.slots),
            "terminal_price_dkk_per_kwh": (
                round(float(terminal_price_dkk_per_kwh), 4)
                if terminal_price_dkk_per_kwh is not None
                else None
            ),
            **totals,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            "slots": [slot.as_dict() for slot in self.slots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyPlan":
        slots: list[PlannedSlot] = []
        for raw in data.get("slots", []):
            try:
                slots.append(
                    PlannedSlot(
                        start=datetime.fromisoformat(raw["start"]),
                        end=datetime.fromisoformat(raw["end"]),
                        action=Action(raw["action"]),
                        price=float(raw.get("price", 0.0)),
                        expected_load_wh=float(raw.get("expected_load_wh", 0.0)),
                        grid_import_wh=float(raw.get("grid_import_wh", 0.0)),
                        battery_charge_wh=float(raw.get("battery_charge_wh", 0.0)),
                        battery_discharge_wh=float(
                            raw.get("battery_discharge_wh", 0.0)
                        ),
                        soc_start=float(raw.get("soc_start", 0.0)),
                        soc_end=float(raw.get("soc_end", 0.0)),
                        interval_cost_dkk=float(raw.get("interval_cost_dkk", 0.0)),
                        baseline_cost_dkk=float(raw.get("baseline_cost_dkk", 0.0)),
                        reason=raw.get("reason", ""),
                        price_source=raw.get("price_source", "known"),
                        price_uncertainty_dkk_per_kwh=float(
                            raw.get("price_uncertainty_dkk_per_kwh", 0.0)
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        created_at: datetime | None = None
        if isinstance(data.get("created_at"), str):
            try:
                created_at = datetime.fromisoformat(data["created_at"])
            except ValueError:
                created_at = None
        return cls(
            date=str(data.get("date", "")),
            slots=tuple(slots),
            created_at=created_at,
        )
