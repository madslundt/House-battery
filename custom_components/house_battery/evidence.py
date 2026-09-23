"""Continuous interval accounting and conservative battery learning."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from math import sqrt

from .accounting import IntervalAccumulator
from .const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
)
from .models import Action, GRID_POWER_IMPORT_POSITIVE
from .runtime import RuntimeState


class EvidenceCollector:
    """Turn live power samples into durable learning and financial evidence."""

    def __init__(
        self,
        runtime: Callable[[], RuntimeState],
        value: Callable[[str, float | None], float | None],
        save: Callable[[], Awaitable[None]],
    ) -> None:
        self._runtime = runtime
        self._value = value
        self._save = save
        self._accumulator: IntervalAccumulator | None = None
        self._last_sample_at: datetime | None = None
        self._battery_sample_at: datetime | None = None
        self._session_action: Action | None = None
        self._session_start_soc: float | None = None
        self._session_energy_wh = 0.0

    async def async_observe(
        self, now: datetime, price: float | None, action: Action, soc: float
    ) -> None:
        await self._learn_battery(now, action, soc)
        await self._account(now, price, action, soc)

    async def _account(
        self, now: datetime, price: float | None, action: Action, soc: float
    ) -> None:
        runtime = self._runtime()
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        if self._accumulator and self._accumulator.start != bucket:
            completed = self._accumulator
            runtime.ledger.close(
                completed, runtime.settings["degradation_cost_dkk_per_kwh"]
            )
            if completed.seconds > 0:
                runtime.load_learner.observe(
                    completed.start, completed.load_wh * 3600 / completed.seconds
                )
            self._accumulator = None
            await self._save()
        if self._accumulator is None:
            self._accumulator = IntervalAccumulator(bucket)
        elapsed = (
            min(60.0, max(0.0, (now - self._last_sample_at).total_seconds()))
            if self._last_sample_at
            else 0
        )
        self._last_sample_at = now
        self._accumulator.add(
            seconds=elapsed,
            load_w=self._value(CONF_LOAD_POWER, 0) or 0,
            grid_power_w=self._value(CONF_GRID_IMPORT_POWER, 0) or 0,
            grid_sign=GRID_POWER_IMPORT_POSITIVE,
            charge_w=self._value(CONF_BATTERY_CHARGE_POWER, 0) or 0,
            discharge_w=self._value(CONF_BATTERY_DISCHARGE_POWER, 0) or 0,
            price=price,
            soc=soc,
            action=action,
        )

    async def _learn_battery(self, now: datetime, action: Action, soc: float) -> None:
        elapsed = (
            min(120.0, max(0.0, (now - self._battery_sample_at).total_seconds()))
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
        if action is Action.CHARGE:
            power = self._value(CONF_BATTERY_CHARGE_POWER, 0) or 0
        elif action is Action.BATTERY:
            power = self._value(CONF_BATTERY_DISCHARGE_POWER, 0) or 0
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
