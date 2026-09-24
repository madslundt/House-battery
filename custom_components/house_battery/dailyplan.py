"""Persisted, reconciled daily optimization timeline.

The optimizer (:mod:`planner`) only ever plans the *future*: from ``now`` to the
end of the known/forecast price horizon.  The dashboard, however, must always
show a complete, uninterrupted overview, so this module bridges the two by
keeping a small, timezone-aware, persisted timeline that

* starts at the beginning of the current local day and extends through the
  whole known-price horizon (today plus any following days whose prices are
  already available),
* never rewrites the already-published past (intervals before the replan
  ``cutoff`` are immutable),
* replaces only the future portion on every replan, and
* stays invariant-clean (sorted, non-overlapping, non-duplicate, non-zero
  duration, ``start < end``, bounded to ``[day_start, horizon_end]``). The
  timeline is also normalised to a single local timezone offset so its
  timestamps are never ambiguous.

The stored timeline keeps one slot per price interval so the economic detail
(per-slot cost/energy/SOC) stays correct.  Adjacent identical-action slots are
merged lazily for display via :func:`merge_adjacent_blocks`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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


def _clip_to_window(
    slot: PlannedSlot, start: datetime, end: datetime, tz: timezone
) -> PlannedSlot | None:
    """Clip a slot to ``[start, end)`` and normalise to a single local offset.

    ``start``/``end`` may arrive in a different timezone than the slot itself
    (the price feed is commonly UTC while the day anchor is local).  The
    comparison uses the instants, but the *returned* endpoints are expressed in
    ``tz`` so every stored interval shares one offset and never mixes ``+00:00``
    on its start with ``+02:00`` on its end.
    """

    new_start = _to_zone(max(slot.start, start), tz)
    new_end = _to_zone(min(slot.end, end), tz)
    if new_start >= new_end:
        return None
    return replace(slot, start=new_start, end=new_end)


def _to_zone(value: datetime, tz: timezone) -> datetime:
    """Return ``value`` in ``tz`` without changing the underlying instant."""

    return value.astimezone(tz)


def _prorate_head(
    slot: PlannedSlot,
    cutoff: datetime,
    day_start: datetime,
    horizon_end: datetime,
    tz: timezone,
) -> PlannedSlot | None:
    """Truncate a straddling interval to ``[slot.start, cutoff)`` with proration.

    A replan cuts an already-published interval in two.  The retained head must
    stay economically and physically consistent with its *shortened* duration,
    so every extensive quantity is scaled by the fraction of the interval that
    survives (``fraction = retained / total``) and the end SOC is interpolated
    linearly between the original SOC endpoints instead of kept at its
    end-of-interval value.  The result is bounded to ``[day_start, horizon_end]``
    and normalised to the local offset.

    ``fraction`` is exactly ``1.0`` for an interval kept whole, so this helper
    is also safe to reuse for window clipping without altering whole intervals.
    """
    end = _to_zone(min(cutoff, horizon_end), tz)
    start = _to_zone(max(slot.start, day_start), tz)
    if start >= end:
        return None
    total = (slot.end - slot.start).total_seconds()
    retained = (end - start).total_seconds()
    fraction = retained / total if total > 0 else 0.0
    if fraction <= 0:
        return None
    # SOC changes roughly linearly with energy under a constant-power interval,
    # so a shortened interval ends partway between the original SOC endpoints.
    soc_end = slot.soc_start + (slot.soc_end - slot.soc_start) * fraction
    return replace(
        slot,
        start=start,
        end=end,
        expected_load_wh=slot.expected_load_wh * fraction,
        grid_import_wh=slot.grid_import_wh * fraction,
        battery_charge_wh=slot.battery_charge_wh * fraction,
        battery_discharge_wh=slot.battery_discharge_wh * fraction,
        interval_cost_dkk=slot.interval_cost_dkk * fraction,
        baseline_cost_dkk=slot.baseline_cost_dkk * fraction,
        soc_start=slot.soc_start,
        soc_end=soc_end,
    )


def _history_before(
    slots: tuple[PlannedSlot, ...],
    cutoff: datetime,
    day_start: datetime,
    horizon_end: datetime,
    tz: timezone,
) -> list[PlannedSlot]:
    """Keep the published past, truncating any interval that straddles ``cutoff``.

    Intervals ending at or before ``cutoff`` are kept whole.  Intervals starting
    at or after ``cutoff`` are dropped (they belong to the mutable future).  An
    interval that spans ``cutoff`` is truncated to ``[start, cutoff)`` and every
    extensive quantity is prorated to the shortened head so the immutable past
    stays consistent with its reduced duration.  The timeline never overlaps or
    duplicates across the replan point, everything is bounded to
    ``[day_start, horizon_end]`` and normalised to a local offset.
    """
    kept: list[PlannedSlot] = []
    for slot in slots:
        if slot.end <= cutoff:
            clipped = _clip_to_window(slot, day_start, horizon_end, tz)
            if clipped is not None:
                kept.append(clipped)
        elif slot.start >= cutoff:
            continue
        else:
            clipped = _prorate_head(slot, cutoff, day_start, horizon_end, tz)
            if clipped is not None:
                kept.append(clipped)
    return kept


def _future_clipped(
    slots: tuple[PlannedSlot, ...],
    cutoff: datetime,
    day_start: datetime,
    horizon_end: datetime,
    tz: timezone,
) -> list[PlannedSlot]:
    """Keep only the optimizer's future intervals, clipped to the horizon."""
    clipped: list[PlannedSlot] = []
    for slot in slots:
        # The optimizer starts at ``now``; anything at/after ``cutoff`` is the
        # mutable future.  Intervals before the cutoff are impossible here and
        # are ignored defensively.
        if slot.start < cutoff:
            continue
        kept = _clip_to_window(slot, day_start, horizon_end, tz)
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
    horizon_end: datetime,
) -> DailyPlan:
    """Rebuild the timeline from ``day_start`` to ``horizon_end`` at ``cutoff``.

    ``existing`` is the previously persisted timeline.  Its past (before
    ``cutoff``) is preserved exactly; its future is discarded and replaced by
    ``future_slots`` (the optimizer's result).  The timeline starts at the
    ``day_start`` (the beginning of the current local day) and extends through
    the whole ``horizon_end`` (the end of the known-price horizon, so following
    days whose prices are already known are shown too).  A boundary replan
    (``cutoff`` landing on an interval edge) yields no duplicate or
    zero-duration interval.  If the local day has rolled over, the previous
    day's timeline is never merged into the new one.

    Every resulting slot is normalised to the local timezone offset of
    ``day_start`` so the whole timeline shares one, unambiguous offset.
    """

    tz = day_start.tzinfo or timezone.utc
    today = day_start.date().isoformat()
    if existing is None or existing.date != today:
        history: list[PlannedSlot] = []
    else:
        history = _history_before(existing.slots, cutoff, day_start, horizon_end, tz)
    future = _future_clipped(future_slots, cutoff, day_start, horizon_end, tz)
    normalized = _normalize(history + future)
    return DailyPlan(date=today, slots=tuple(normalized), created_at=cutoff)


def merge_adjacent_blocks(slots: tuple[PlannedSlot, ...]) -> list[PlannedSlot]:
    """Merge contiguous slots that share the same effective operating state.

    Two intervals are merged only when they touch (``a.end == b.start``) **and**
    carry the same action, price and reason.  Merging merely on the action is
    dropped on purpose: a merged block exposes a single ``price`` and ``reason``
    metadata field, so combining, say, a low-price charge with a high-price
    charge (or two charge windows with justifications that disagree) would
    falsely imply the whole merged block ran at one price for one reason.  Power,
    expected load and energy are intensive to the operating state and are summed
    as a faithful aggregate over the merged span; the merged SOC ``start``/``end``
    are the endpoints of a contiguous SOC curve and stay correct.
    """
    ordered = sorted((slot for slot in slots if slot.start < slot.end), key=lambda s: s.start)
    blocks: list[PlannedSlot] = []
    for slot in ordered:
        if (
            blocks
            and slot.start == blocks[-1].end
            and blocks[-1].action is slot.action
            and abs(blocks[-1].price - slot.price) <= 1e-9
            and blocks[-1].reason == slot.reason
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
        actual_soc_at: str | None = None,
        terminal_price_dkk_per_kwh: float | None = None,
    ) -> dict[str, Any]:
        """Return the dashboard-ready representation of the daily timeline.

        The blocks merge adjacent compatible actions so the timeline reads as a
        sequence of operating periods rather than raw price intervals.  The
        observed SOC and the moment it was taken are surfaced as first-class,
        top-level fields rather than attached to the first block: the first
        published block starts at local 00:00 (or at ``history_available_from``
        for a mid-day cold start), so binding a *current* observation to that
        midnight block would misreport when the reading was taken.  The future
        SOC curve already begins at the re-anchored observed SOC, so the
        ``actual_soc``/``actual_soc_at`` pair lets the dashboard overlay the real
        reading against the planned curve without ever altering the published
        block data.
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
        # The earliest published slot marks where history actually begins: the
        # local midnight today once history is persisted, or the replan cutoff on
        # a mid-day cold start (everything before it is "unavailable history").
        history_available_from = (
            self.slots[0].start.isoformat() if self.slots else None
        )
        return {
            "date": self.date,
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            "actual_soc": float(actual_soc) if actual_soc is not None else None,
            "actual_soc_at": actual_soc_at,
            "history_available_from": history_available_from,
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
