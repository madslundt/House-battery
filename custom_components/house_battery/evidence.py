"""Continuous interval accounting and conservative battery learning.

Financial accounting and battery learning require a balanced measured
:class:`~house_battery.models.PowerFlowSnapshot`. Connected-load learning is
independent and continues when a grid meter is unavailable. An absent grid
meter never invents zero-energy or battery-flow evidence.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from math import isfinite, sqrt

from .accounting import IntervalAccumulator
from .models import Action, GRID_POWER_IMPORT_POSITIVE, PowerFlowSnapshot
from .runtime import RuntimeState


class EvidenceCollector:
    """Turn live power samples into durable learning and financial evidence."""

    def __init__(
        self,
        runtime: Callable[[], RuntimeState],
        flow: Callable[[], PowerFlowSnapshot | None],
        save: Callable[[], Awaitable[None]],
        load_power: Callable[[], float | None],
    ) -> None:
        self._runtime = runtime
        self._flow = flow
        self._save = save
        self._load_power = load_power
        self._accumulator: IntervalAccumulator | None = None
        self._last_sample_at: datetime | None = None
        self._battery_sample_at: datetime | None = None
        self._load_bucket_start: datetime | None = None
        self._load_bucket_wh = 0.0
        self._load_bucket_seconds = 0.0
        self._last_load_sample_at: datetime | None = None
        self._session_action: Action | None = None
        self._session_start_soc: float | None = None
        self._session_energy_wh = 0.0

    async def async_observe(
        self, now: datetime, price: float | None, action: Action, soc: float
    ) -> None:
        flow = self._flow()
        await self._learn_load(now, self._load_power())
        await self._learn_battery(now, action, soc, flow)
        await self._account(now, price, action, soc, flow)

    async def _learn_load(self, now: datetime, watts: float | None) -> None:
        """Learn connected load independently of grid/battery-flow evidence."""
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        runtime = self._runtime()
        if self._load_bucket_start and self._load_bucket_start != bucket:
            if self._load_bucket_seconds > 0:
                runtime.load_learner.observe(
                    self._load_bucket_start,
                    self._load_bucket_wh * 3600 / self._load_bucket_seconds,
                )
                await self._save()
            self._load_bucket_start = None
            self._load_bucket_wh = 0.0
            self._load_bucket_seconds = 0.0
        if self._load_bucket_start is None:
            self._load_bucket_start = bucket
        elapsed = (
            min(60.0, max(0.0, (now - self._last_load_sample_at).total_seconds()))
            if self._last_load_sample_at
            else 0.0
        )
        self._last_load_sample_at = now
        if watts is not None and isfinite(watts) and elapsed > 0:
            self._load_bucket_wh += max(0.0, watts) * elapsed / 3600
            self._load_bucket_seconds += elapsed

    async def _account(
        self,
        now: datetime,
        price: float | None,
        action: Action,
        soc: float,
        flow: PowerFlowSnapshot | None,
    ) -> None:
        runtime = self._runtime()
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        if self._accumulator and self._accumulator.start != bucket:
            completed = self._accumulator
            runtime.ledger.close(
                completed, runtime.settings["degradation_cost_dkk_per_kwh"]
            )
            self._accumulator = None
            await self._save()
        # Without a canonical flow there is no trustworthy load/grid/battery
        # balance, so skip this refresh entirely rather than recording zeros.
        if flow is None or not flow.flow_available:
            return
        if self._accumulator is None:
            self._accumulator = IntervalAccumulator(bucket)
        elapsed = (
            min(
                60.0,
                max(0.0, (now - self._last_sample_at).total_seconds()),
            )
            if self._last_sample_at
            else 0
        )
        self._last_sample_at = now
        self._accumulator.add(
            seconds=elapsed,
            load_w=flow.load_w,
            grid_power_w=flow.grid_power_w,
            grid_sign=GRID_POWER_IMPORT_POSITIVE,
            charge_w=flow.battery_charge_power_w,
            discharge_w=flow.battery_output_power_w,
            price=price,
            soc=flow.soc,
            action=action,
        )

    async def _learn_battery(
        self,
        now: datetime,
        action: Action,
        soc: float,
        flow: PowerFlowSnapshot | None,
    ) -> None:
        elapsed = (
            min(
                120.0,
                max(0.0, (now - self._battery_sample_at).total_seconds()),
            )
            if self._battery_sample_at
            else 0.0
        )
        self._battery_sample_at = now
        if action != self._session_action:
            await self._finish_battery_session(soc)
            self._session_action = action
            self._session_start_soc = soc
            self._session_energy_wh = 0.0
        power = 0.0
        if flow is not None and flow.flow_available:
            if action is Action.CHARGE:
                power = flow.battery_charge_power_w
            elif action is Action.BATTERY:
                power = flow.battery_output_power_w
        self._session_energy_wh += max(0.0, power) * elapsed / 3600

    async def _finish_battery_session(self, end_soc: float) -> None:
        if self._session_action is None or self._session_start_soc is None:
            return
        movement = abs(end_soc - self._session_start_soc)
        if movement < 20 or self._session_energy_wh <= 0:
            return
        runtime = self._runtime()
        fallback_efficiency = runtime.settings["round_trip_efficiency"] / 100
        if self._session_action is Action.BATTERY and end_soc < self._session_start_soc:
            capacity = (
                self._session_energy_wh / (movement / 100) / sqrt(fallback_efficiency)
            )
            runtime.battery_learner.add_capacity_sample(capacity, movement)
        elif (
            self._session_action is Action.CHARGE and end_soc > self._session_start_soc
        ):
            capacity = runtime.battery_learner.learned_capacity_wh
            if capacity is None:
                return
            charge_efficiency = capacity * (movement / 100) / self._session_energy_wh
            runtime.battery_learner.add_efficiency_sample(
                charge_efficiency * sqrt(fallback_efficiency), movement
            )
        await self._save()
