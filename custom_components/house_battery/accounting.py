"""Local interval ledger and conservative savings accounting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from .const import LEDGER_HISTORY_LIMIT
from .models import Action, normalize_grid_flow


@dataclass(slots=True)
class IntervalAccumulator:
    """Weighted telemetry accumulated within one UTC quarter-hour.

    Grid power is accepted as a *signed* value (positive = import, negative =
    export) and decomposed into import and export energy here, at this single
    normalisation point. Export is accumulated in its own field and is never
    clamped away, so any meaningful export stays visible in the ledger.
    """

    start: datetime
    seconds: float = 0.0
    load_wh: float = 0.0
    grid_import_wh: float = 0.0
    grid_export_wh: float = 0.0
    charge_wh: float = 0.0
    discharge_wh: float = 0.0
    price_seconds: float = 0.0
    price_weighted: float = 0.0
    flow_seconds: float = 0.0
    flow_samples: int = 0
    battery_seconds: float = 0.0
    battery_samples: int = 0
    charge_cost_dkk: float = 0.0
    discharge_value_dkk: float = 0.0
    charge_priced_wh: float = 0.0
    discharge_priced_wh: float = 0.0
    soc_first: float | None = None
    soc_last: float | None = None
    samples: int = 0
    action: str = Action.SAFE.value
    grid_sign: float = 1.0

    def add(
        self,
        *,
        seconds: float,
        load_w: float,
        grid_power_w: float,
        charge_w: float,
        discharge_w: float,
        price: float | None,
        soc: float,
        action: Action,
        grid_sign: float = 1.0,
        flow_available: bool = True,
    ) -> None:
        hours = max(0.0, seconds) / 3600
        self.seconds += seconds
        if flow_available:
            self.flow_seconds += seconds
            self.flow_samples += 1
            self.load_wh += max(0.0, load_w) * hours
            import_w, export_w = normalize_grid_flow(grid_power_w, grid_sign)
            self.grid_import_wh += max(0.0, import_w) * hours
            self.grid_export_wh += max(0.0, export_w) * hours
        self.battery_seconds += seconds
        self.battery_samples += 1
        self.charge_wh += max(0.0, charge_w) * hours
        self.discharge_wh += max(0.0, discharge_w) * hours
        if price is not None:
            self.price_seconds += seconds
            self.price_weighted += price * seconds
            charge_wh = max(0.0, charge_w) * hours
            discharge_wh = max(0.0, discharge_w) * hours
            self.charge_cost_dkk += charge_wh / 1000 * price
            self.discharge_value_dkk += discharge_wh / 1000 * price
            self.charge_priced_wh += charge_wh
            self.discharge_priced_wh += discharge_wh
        if self.soc_first is None:
            self.soc_first = soc
        self.soc_last = soc
        self.samples += 1
        self.action = action.value


@dataclass(frozen=True, slots=True)
class LedgerInterval:
    """Completed interval retained for audit and export."""

    start: str
    end: str
    action: str
    price_dkk_per_kwh: float | None
    load_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_charge_kwh: float
    battery_discharge_kwh: float
    soc_start: float | None
    soc_end: float | None
    baseline_cost_dkk: float | None
    actual_cost_dkk: float | None
    degradation_dkk: float
    net_savings_dkk: float | None
    quality: str
    battery_quality: str = "incomplete"
    charge_cost_dkk: float | None = None
    discharge_value_dkk: float | None = None


@dataclass(slots=True)
class EnergyLedger:
    """Bounded evidence ledger, independent of Home Assistant Recorder."""

    intervals: list[LedgerInterval] = field(default_factory=list)
    total_charge_kwh: float = 0.0
    total_discharge_kwh: float = 0.0
    total_charge_cost_dkk: float = 0.0
    total_discharge_value_dkk: float = 0.0
    total_export_kwh: float = 0.0
    total_net_savings_dkk: float = 0.0
    savings_intervals: int = 0

    def close(
        self, accumulator: IntervalAccumulator, degradation_cost: float
    ) -> LedgerInterval:
        price = (
            accumulator.price_weighted / accumulator.price_seconds
            if accumulator.price_seconds > 0
            else None
        )
        coverage = accumulator.flow_seconds / (15 * 60)
        quality = (
            "good"
            if (
                coverage >= 0.75
                and accumulator.flow_samples >= 2
                and price is not None
            )
            else "incomplete"
        )
        battery_coverage = accumulator.battery_seconds / (15 * 60)
        battery_quality = (
            "good"
            if battery_coverage >= 0.75 and accumulator.battery_samples >= 2
            else "incomplete"
        )
        load_kwh = accumulator.load_wh / 1000
        import_kwh = accumulator.grid_import_wh / 1000
        export_kwh = accumulator.grid_export_wh / 1000
        charge_kwh = accumulator.charge_wh / 1000
        discharge_kwh = accumulator.discharge_wh / 1000
        degradation = discharge_kwh * degradation_cost
        baseline = load_kwh * price if price is not None and quality == "good" else None
        # Export is reported, never credited: we do not value exported energy as
        # avoided import (there is no export contract to settle against), so it
        # must not reduce the measured import cost.
        actual = (
            import_kwh * price
            if price is not None and quality == "good"
            else None
        )
        savings = (
            baseline - actual - degradation
            if baseline is not None and actual is not None
            else None
        )
        charge_cost = (
            accumulator.charge_cost_dkk
            if battery_quality == "good"
            and accumulator.charge_priced_wh >= accumulator.charge_wh * 0.99
            else None
        )
        discharge_value = (
            accumulator.discharge_value_dkk
            if battery_quality == "good"
            and accumulator.discharge_priced_wh >= accumulator.discharge_wh * 0.99
            else None
        )
        interval = LedgerInterval(
            start=accumulator.start.isoformat(),
            end=(accumulator.start + timedelta(minutes=15)).isoformat(),
            action=accumulator.action,
            price_dkk_per_kwh=price,
            load_kwh=load_kwh,
            grid_import_kwh=import_kwh,
            grid_export_kwh=export_kwh,
            battery_charge_kwh=charge_kwh,
            battery_discharge_kwh=discharge_kwh,
            soc_start=accumulator.soc_first,
            soc_end=accumulator.soc_last,
            baseline_cost_dkk=baseline,
            actual_cost_dkk=actual,
            degradation_dkk=degradation,
            net_savings_dkk=savings,
            quality=quality,
            battery_quality=battery_quality,
            charge_cost_dkk=charge_cost,
            discharge_value_dkk=discharge_value,
        )
        self.intervals = (self.intervals + [interval])[-LEDGER_HISTORY_LIMIT:]
        if battery_quality == "good":
            self.total_charge_kwh += charge_kwh
            self.total_discharge_kwh += discharge_kwh
            if charge_cost is not None:
                self.total_charge_cost_dkk += charge_cost
            if discharge_value is not None:
                self.total_discharge_value_dkk += discharge_value
        self.total_export_kwh += export_kwh
        if savings is not None:
            self.total_net_savings_dkk += savings
            self.savings_intervals += 1
        return interval

    def totals_between(
        self, start: datetime, end: datetime | None = None
    ) -> dict[str, float | None]:
        """Return measured totals for a half-open calendar interval.

        Completed intervals are retained in persistent runtime state, so a
        previous calendar period remains available after a restart.
        """
        selected = [
            item
            for item in self.intervals
            if datetime.fromisoformat(item.start) >= start
            and (end is None or datetime.fromisoformat(item.start) < end)
        ]
        battery_selected = [item for item in selected if item.battery_quality == "good"]
        priced_charge = [
            item for item in battery_selected if item.charge_cost_dkk is not None
        ]
        priced_discharge = [
            item for item in battery_selected if item.discharge_value_dkk is not None
        ]
        savings_selected = [
            item for item in selected if item.net_savings_dkk is not None
        ]
        charge_kwh = sum(item.battery_charge_kwh for item in battery_selected)
        discharge_kwh = sum(item.battery_discharge_kwh for item in battery_selected)
        charge_cost = sum(item.charge_cost_dkk or 0 for item in priced_charge)
        discharge_value = sum(
            item.discharge_value_dkk or 0 for item in priced_discharge
        )
        priced_charge_kwh = sum(item.battery_charge_kwh for item in priced_charge)
        priced_discharge_kwh = sum(
            item.battery_discharge_kwh for item in priced_discharge
        )
        return {
            "charge_kwh": charge_kwh,
            "discharge_kwh": discharge_kwh,
            "charge_cost_dkk": charge_cost if priced_charge else None,
            "discharge_value_dkk": discharge_value if priced_discharge else None,
            "charge_price_dkk_per_kwh": (
                charge_cost / priced_charge_kwh if priced_charge_kwh > 0 else None
            ),
            "discharge_price_dkk_per_kwh": (
                discharge_value / priced_discharge_kwh
                if priced_discharge_kwh > 0
                else None
            ),
            "grid_export_kwh": sum(item.grid_export_kwh for item in selected),
            "net_savings_dkk": (
                sum(item.net_savings_dkk or 0 for item in savings_selected)
                if savings_selected
                else None
            ),
            "baseline_cost_dkk": (
                sum(item.baseline_cost_dkk or 0 for item in savings_selected)
                if savings_selected
                else None
            ),
            "actual_cost_dkk": (
                sum(item.actual_cost_dkk or 0 for item in savings_selected)
                if savings_selected
                else None
            ),
        }

    def totals_since(self, since: datetime) -> dict[str, float | None]:
        """Return measured totals from ``since`` through the retained ledger."""
        return self.totals_between(since)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intervals": [asdict(item) for item in self.intervals],
            "total_charge_kwh": self.total_charge_kwh,
            "total_discharge_kwh": self.total_discharge_kwh,
            "total_charge_cost_dkk": self.total_charge_cost_dkk,
            "total_discharge_value_dkk": self.total_discharge_value_dkk,
            "total_export_kwh": self.total_export_kwh,
            "total_net_savings_dkk": self.total_net_savings_dkk,
            "savings_intervals": self.savings_intervals,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnergyLedger:
        intervals = []
        for value in data.get("intervals", []):
            # Keep grid_export_kwh: it is now a first-class ledger field. Ledger
            # intervals persisted by the 1.3.x line predate the field, so default
            # it to 0.0 to keep old ledgers loadable across the upgrade.
            interval = dict(value)
            interval.setdefault("grid_export_kwh", 0.0)
            interval.setdefault(
                "battery_quality", interval.get("quality", "incomplete")
            )
            interval.setdefault("charge_cost_dkk", None)
            interval.setdefault("discharge_value_dkk", None)
            intervals.append(LedgerInterval(**interval))
        total_net_savings = float(data.get("total_net_savings_dkk", 0))
        return cls(
            intervals=intervals[-LEDGER_HISTORY_LIMIT:],
            total_charge_kwh=float(data.get("total_charge_kwh", 0)),
            total_discharge_kwh=float(data.get("total_discharge_kwh", 0)),
            total_charge_cost_dkk=float(data.get("total_charge_cost_dkk", 0)),
            total_discharge_value_dkk=float(data.get("total_discharge_value_dkk", 0)),
            total_export_kwh=float(data.get("total_export_kwh", 0)),
            total_net_savings_dkk=total_net_savings,
            savings_intervals=max(
                0,
                int(
                    data.get(
                        "savings_intervals",
                        1 if total_net_savings != 0 else 0,
                    )
                ),
            ),
        )


def calendar_period_bounds(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Return local calendar-period bounds for dashboard accounting sensors."""
    if now.tzinfo is None:
        raise ValueError("Calendar periods require an aware local datetime")

    def midnight(value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=now.tzinfo)

    today = now.date()
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    month_start = today.replace(day=1)
    last_month_end = month_start
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    return {
        "today": (midnight(today), midnight(tomorrow)),
        "yesterday": (midnight(yesterday), midnight(today)),
        "week": (midnight(week_start), midnight(today + timedelta(days=1))),
        "last_week": (midnight(last_week_start), midnight(week_start)),
        "month": (midnight(month_start), midnight(tomorrow)),
        "last_month": (midnight(last_month_start), midnight(last_month_end)),
    }
