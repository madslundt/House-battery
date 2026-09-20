"""Local interval ledger and conservative savings accounting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .const import LEDGER_HISTORY_LIMIT
from .models import Action


@dataclass(slots=True)
class IntervalAccumulator:
    """Weighted telemetry accumulated within one UTC quarter-hour."""

    start: datetime
    seconds: float = 0.0
    load_wh: float = 0.0
    grid_import_wh: float = 0.0
    grid_export_wh: float = 0.0
    charge_wh: float = 0.0
    discharge_wh: float = 0.0
    price_seconds: float = 0.0
    price_weighted: float = 0.0
    soc_first: float | None = None
    soc_last: float | None = None
    samples: int = 0
    action: str = Action.SAFE.value

    def add(
        self,
        *,
        seconds: float,
        load_w: float,
        grid_import_w: float,
        grid_export_w: float,
        charge_w: float,
        discharge_w: float,
        price: float | None,
        soc: float,
        action: Action,
    ) -> None:
        hours = max(0.0, seconds) / 3600
        self.seconds += seconds
        self.load_wh += max(0.0, load_w) * hours
        self.grid_import_wh += max(0.0, grid_import_w) * hours
        self.grid_export_wh += max(0.0, grid_export_w) * hours
        self.charge_wh += max(0.0, charge_w) * hours
        self.discharge_wh += max(0.0, discharge_w) * hours
        if price is not None:
            self.price_seconds += seconds
            self.price_weighted += price * seconds
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


@dataclass(slots=True)
class EnergyLedger:
    """Bounded evidence ledger, independent of Home Assistant Recorder."""

    intervals: list[LedgerInterval] = field(default_factory=list)
    total_charge_kwh: float = 0.0
    total_discharge_kwh: float = 0.0
    total_net_savings_dkk: float = 0.0

    def close(
        self, accumulator: IntervalAccumulator, degradation_cost: float
    ) -> LedgerInterval:
        price = (
            accumulator.price_weighted / accumulator.price_seconds
            if accumulator.price_seconds > 0
            else None
        )
        coverage = accumulator.seconds / (15 * 60)
        quality = (
            "good"
            if coverage >= 0.75 and accumulator.samples >= 2 and price is not None
            else "incomplete"
        )
        load_kwh = accumulator.load_wh / 1000
        import_kwh = accumulator.grid_import_wh / 1000
        export_kwh = accumulator.grid_export_wh / 1000
        charge_kwh = accumulator.charge_wh / 1000
        discharge_kwh = accumulator.discharge_wh / 1000
        degradation = discharge_kwh * degradation_cost
        baseline = load_kwh * price if price is not None and quality == "good" else None
        actual = (
            (import_kwh - export_kwh) * price
            if price is not None and quality == "good"
            else None
        )
        savings = (
            baseline - actual - degradation
            if baseline is not None and actual is not None
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
        )
        self.intervals = (self.intervals + [interval])[-LEDGER_HISTORY_LIMIT:]
        self.total_charge_kwh += charge_kwh
        self.total_discharge_kwh += discharge_kwh
        if savings is not None:
            self.total_net_savings_dkk += savings
        return interval

    def totals_since(self, since: datetime) -> dict[str, float]:
        selected = [
            item
            for item in self.intervals
            if datetime.fromisoformat(item.start) >= since
        ]
        return {
            "charge_kwh": sum(item.battery_charge_kwh for item in selected),
            "discharge_kwh": sum(item.battery_discharge_kwh for item in selected),
            "net_savings_dkk": sum(item.net_savings_dkk or 0 for item in selected),
            "baseline_cost_dkk": sum(item.baseline_cost_dkk or 0 for item in selected),
            "actual_cost_dkk": sum(item.actual_cost_dkk or 0 for item in selected),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "intervals": [asdict(item) for item in self.intervals],
            "total_charge_kwh": self.total_charge_kwh,
            "total_discharge_kwh": self.total_discharge_kwh,
            "total_net_savings_dkk": self.total_net_savings_dkk,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnergyLedger:
        intervals = []
        for value in data.get("intervals", []):
            try:
                intervals.append(LedgerInterval(**value))
            except (TypeError, ValueError):
                continue
        return cls(
            intervals=intervals[-LEDGER_HISTORY_LIMIT:],
            total_charge_kwh=float(data.get("total_charge_kwh", 0)),
            total_discharge_kwh=float(data.get("total_discharge_kwh", 0)),
            total_net_savings_dkk=float(data.get("total_net_savings_dkk", 0)),
        )
