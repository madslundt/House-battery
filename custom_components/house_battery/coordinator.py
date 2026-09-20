"""Coordinator for learning, planning, accounting, and guarded execution."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .actuator import LocalControlAdapter, soc_control_problems
from .const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_COMMISSIONED,
    CONF_GRID_AVAILABLE,
    CONF_GRID_EXPORT_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_PV_POWER,
    CONF_SOC,
    DECISION_HISTORY_LIMIT,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .evidence import EvidenceCollector
from .forecast import assess_external_forecast, extend_known_horizon
from .health import get_health_problems
from .models import Action, Plan, PlannerSettings, PriceSlot
from .planner import optimize
from .policy import (
    StoragePolicy,
    action_from_operating_mode,
    apply_storage_policy,
    parse_grid_available,
)
from .price import extract_rows, normalize_price_rows
from .runtime import RuntimeState, RuntimeStore

_LOGGER = logging.getLogger(__name__)
_BAD_STATES = {"unknown", "unavailable", "none", ""}


class Fbp1200Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Present one deep interface over the optimizer implementation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry
        self.store = RuntimeStore(hass, entry.entry_id)
        self.runtime = RuntimeState()
        self.plan: Plan | None = None
        self._startup_guard_passed = False
        self._last_decision_key: tuple[str, str] | None = None
        self._forecast_evidence_changed = False
        self._forecast_slot_count = 0
        self._forecast_used_slot_count = 0
        self._forecast_source: str | None = None
        self._forecast_status = "not_configured"
        self._forecast_last_updated: str | None = None
        self.actuator = LocalControlAdapter(
            hass,
            lambda: self.config,
            lambda: self.runtime,
            lambda: self.store.save(self.runtime),
        )
        self.evidence = EvidenceCollector(
            lambda: self.runtime,
            self._float,
            lambda: self.store.save(self.runtime),
        )

    @property
    def config(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            manufacturer="FOSSiBOT",
            model="FBP1200",
            name=self.entry.title,
            configuration_url="https://github.com/madslundt/House-battery",
        )

    async def async_initialize(self) -> None:
        self.runtime = await self.store.load()
        self.runtime.load_learner.configure_time_zone(
            dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        )
        if self.runtime.execution_enabled:
            problems = soc_control_problems(
                self.hass,
                self.config,
                absolute_min_soc=self.runtime.settings["absolute_min_soc"],
                maximum_soc=self.runtime.settings["opportunistic_target_soc"],
            )
            if not self.config.get(CONF_COMMISSIONED, False) or problems:
                self.runtime.execution_enabled = False
                await self.store.save(self.runtime)

    def _state(self, key: str):
        entity_id = self.config.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def _float(self, key: str, default: float | None = None) -> float | None:
        state = self._state(key)
        try:
            return (
                float(state.state)
                if state and state.state.lower() not in _BAD_STATES
                else default
            )
        except (TypeError, ValueError):
            return default

    def _grid_available(self) -> bool | None:
        """Read physical grid availability; never infer it from grid import power."""
        state = self._state(CONF_GRID_AVAILABLE)
        return parse_grid_available(state.state if state else None)

    def _price_slots(self, now: datetime) -> list[PriceSlot]:
        rows: list[Any] = []
        for entity_id in self.config.get(CONF_PRICE_ENTITIES, []):
            state = self.hass.states.get(entity_id)
            if state:
                rows.extend(extract_rows(dict(state.attributes)))
        known_slots = normalize_price_rows(rows)
        forecast_slots: list[PriceSlot] = []
        forecast_entity = self.config.get(CONF_PRICE_FORECAST_ENTITY)
        self._forecast_source = forecast_entity
        forecast_state = (
            self.hass.states.get(forecast_entity) if forecast_entity else None
        )
        self._forecast_last_updated = None
        if forecast_entity is None:
            self._forecast_status = "not_configured"
        elif forecast_state is None or forecast_state.state.lower() in _BAD_STATES:
            self._forecast_status = "unavailable"
        else:
            reported_at = (
                getattr(forecast_state, "last_reported", None)
                or forecast_state.last_updated
            )
            self._forecast_last_updated = reported_at.isoformat()
            assessment = assess_external_forecast(
                extract_rows(dict(forecast_state.attributes)),
                now=now,
                reported_at=reported_at,
                maximum_age=timedelta(
                    minutes=self.runtime.settings["forecast_max_age_minutes"]
                ),
            )
            self._forecast_status = assessment.status
            forecast_slots = list(assessment.slots)
        self._forecast_slot_count = len(forecast_slots)
        before = self.runtime.forecast_accuracy.as_dict()
        if forecast_slots:
            self.runtime.forecast_accuracy.record_forecasts(forecast_slots)
            self.runtime.forecast_accuracy.score_actual_prices(
                known_slots,
                uncertainty_dkk_per_kwh=self.runtime.settings[
                    "forecast_uncertainty_dkk_per_kwh"
                ],
            )
        self._forecast_evidence_changed = (
            before != self.runtime.forecast_accuracy.as_dict()
        )
        slots = (
            extend_known_horizon(
                known_slots,
                forecast_slots,
                uncertainty_dkk_per_kwh=self.runtime.settings[
                    "forecast_uncertainty_dkk_per_kwh"
                ],
            )
            if self.runtime.forecast_enabled and forecast_slots
            else known_slots
        )
        self._forecast_used_slot_count = sum(
            slot.source == "forecast" for slot in slots
        )
        if self._forecast_status == "available":
            if not self.runtime.forecast_enabled:
                self._forecast_status = "disabled"
            elif not self._forecast_used_slot_count:
                self._forecast_status = "no_contiguous_extension"
            else:
                self._forecast_status = "used"
        result: list[PriceSlot] = []
        for slot in slots:
            if slot.end <= now:
                continue
            load_w = self.runtime.load_learner.predict_w(
                slot.start, self._float(CONF_LOAD_POWER, 0) or 0
            )
            scheduled_wh = self._scheduled_load_wh(slot.start, slot.end)
            result.append(
                PriceSlot(
                    slot.start,
                    slot.end,
                    slot.price,
                    expected_load_wh=load_w * slot.hours + scheduled_wh,
                    expected_pv_wh=0.0,
                    source=slot.source,
                    uncertainty_dkk_per_kwh=slot.uncertainty_dkk_per_kwh,
                )
            )
        return result

    def _scheduled_load_wh(self, start: datetime, end: datetime) -> float:
        total = 0.0
        keep: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for item in self.runtime.scheduled_loads:
            try:
                item_start = datetime.fromisoformat(item["start"])
                item_end = datetime.fromisoformat(item["end"])
                if item_end > now:
                    keep.append(item)
                overlap = max(
                    0.0, (min(end, item_end) - max(start, item_start)).total_seconds()
                )
                total += float(item["additional_w"]) * overlap / 3600
            except (KeyError, TypeError, ValueError):
                continue
        self.runtime.scheduled_loads = keep
        return total

    def _settings(self) -> PlannerSettings:
        values = self.runtime.settings
        capacity = (
            self.runtime.battery_learner.learned_capacity_wh
            or values["capacity_kwh"] * 1000
        )
        efficiency = (
            self.runtime.battery_learner.learned_efficiency
            or values["round_trip_efficiency"] / 100
        )
        return PlannerSettings(
            capacity_wh=capacity,
            reserve_soc=values["reserve_soc"],
            target_soc=values["target_soc"],
            charge_power_w=values["charge_power_w"],
            discharge_power_w=values["discharge_power_w"],
            round_trip_efficiency=efficiency,
            degradation_cost_dkk_per_kwh=values["degradation_cost_dkk_per_kwh"],
            minimum_profit_dkk_per_kwh=values["minimum_profit_dkk_per_kwh"],
            switching_penalty_dkk=values["switching_penalty_dkk"],
            minimum_mode_minutes=round(values["minimum_mode_minutes"]),
            maximum_transitions=round(values["maximum_transitions_per_day"]),
        )

    def _observed_action(self) -> Action:
        state = self._state(CONF_OPERATING_MODE)
        return action_from_operating_mode(state.state if state else None)

    def _current_price(self, slots: list[PriceSlot], now: datetime) -> float | None:
        for slot in slots:
            if slot.start <= now < slot.end:
                return slot.price
        for entity_id in self.config.get(CONF_PRICE_ENTITIES, []):
            state = self.hass.states.get(entity_id)
            try:
                return float(state.state) if state else None
            except (TypeError, ValueError):
                continue
        return None

    async def _force_safe_if_needed(self, now: datetime) -> str:
        if not self.runtime.execution_enabled:
            return "automatic writes disabled"
        success, result = await self.actuator.async_command(Action.SAFE, now)
        self.runtime.execution_enabled = False
        await self.store.save(self.runtime)
        if success:
            return f"{result}; automatic control latched off"
        return result

    async def _async_update_data(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        problems = get_health_problems(self.hass, self.config, now)
        soc = self._float(CONF_SOC)
        slots = self._price_slots(now)
        action = self._observed_action()
        grid_available = self._grid_available()
        price = self._current_price(slots, now)
        if soc is not None:
            await self.evidence.async_observe(now, price, action, soc)

        state = "BOOTSTRAP"
        reason = "Waiting for valid local telemetry and price intervals"
        command_result = "no command"
        effective_settings: PlannerSettings | None = None
        storage_policy: StoragePolicy | None = None
        if grid_available is False:
            self.plan = None
            state = "OUTAGE"
            reason = "Grid is unavailable; optimizer is preserving the absolute emergency SOC"
            command_result = await self._force_safe_if_needed(now)
        elif problems:
            state = "DEGRADED"
            reason = "; ".join(problems)
            command_result = await self._force_safe_if_needed(now)
        elif soc is not None:
            try:
                effective_settings, storage_policy = apply_storage_policy(
                    self._settings(),
                    slots,
                    extra_storage_spread_dkk_per_kwh=self.runtime.settings[
                        "extra_storage_spread_dkk_per_kwh"
                    ],
                    opportunistic_target_soc=self.runtime.settings[
                        "opportunistic_target_soc"
                    ],
                )
                self.plan = optimize(
                    slots,
                    now=now,
                    soc=soc,
                    settings=effective_settings,
                    current_action=action if action is not Action.SAFE else Action.GRID,
                    mode_lock_remaining_minutes=self.runtime.mode_lock_remaining(now),
                    transitions_used=self.runtime.transitions_used(now),
                )
            except ValueError as exc:
                self.plan = None
                problems.append(str(exc))
            if not self.plan or not self.plan.slots:
                state = "DEGRADED"
                reason = self.plan.reason if self.plan else "; ".join(problems)
                command_result = await self._force_safe_if_needed(now)
            else:
                commissioned = bool(self.config.get(CONF_COMMISSIONED, False))
                state = (
                    "ACTIVE"
                    if commissioned and self.runtime.execution_enabled
                    else "SHADOW"
                )
                reason = self.plan.reason
                if not self._startup_guard_passed:
                    self._startup_guard_passed = True
                    command_result = (
                        "startup freshness gate passed; no write on first refresh"
                    )
                elif state == "ACTIVE":
                    command_result = (
                        await self.actuator.async_command(
                            self.plan.current_action,
                            now,
                            target_soc=effective_settings.target_soc,
                        )
                    )[1]

        decision_key = (state, reason)
        decision_changed = decision_key != self._last_decision_key
        if decision_changed:
            self.runtime.decisions.append(
                {
                    "timestamp": now.isoformat(),
                    "state": state,
                    "action": self.plan.current_action.value
                    if self.plan and self.plan.slots
                    else Action.SAFE.value,
                    "reason": reason,
                    "soc": soc,
                    "price": price,
                    "command_result": command_result,
                }
            )
            self.runtime.decisions = self.runtime.decisions[-DECISION_HISTORY_LIMIT:]
            self._last_decision_key = decision_key
        if decision_changed or self._forecast_evidence_changed:
            await self.store.save(self.runtime)

        local_now = dt_util.as_local(now)
        today_start = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC)
        month_start = local_now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC)
        today = self.runtime.ledger.totals_since(today_start)
        month = self.runtime.ledger.totals_since(month_start)
        capacity_kwh = self._settings().capacity_wh / 1000
        nominal_capacity_kwh = self.runtime.settings["capacity_kwh"]
        equivalent_cycles = (
            self.runtime.ledger.total_discharge_kwh / capacity_kwh
            if capacity_kwh
            else 0
        )
        learned_capacity = self.runtime.battery_learner.learned_capacity_wh
        if learned_capacity:
            degradation_pct = max(
                0.0, 100 * (1 - learned_capacity / (nominal_capacity_kwh * 1000))
            )
        else:
            degradation_pct = min(
                20.0,
                20.0 * equivalent_cycles / self.runtime.settings["cycle_life"],
            )
        return {
            "system_state": state,
            "healthy": not problems and grid_available is True,
            "reason": reason,
            "current_action": self.plan.current_action.value
            if self.plan and self.plan.slots
            else Action.SAFE.value,
            "observed_action": action.value,
            "execution_enabled": self.runtime.execution_enabled,
            "commissioned": bool(self.config.get(CONF_COMMISSIONED, False)),
            "grid_available": grid_available,
            "command_result": command_result,
            "soc": soc,
            "load_power_w": self._float(CONF_LOAD_POWER),
            "grid_import_power_w": self._float(CONF_GRID_IMPORT_POWER),
            "grid_export_power_w": self._float(CONF_GRID_EXPORT_POWER, 0),
            "battery_charge_power_w": self._float(CONF_BATTERY_CHARGE_POWER, 0),
            "battery_discharge_power_w": self._float(CONF_BATTERY_DISCHARGE_POWER, 0),
            "pv_power_w": self._float(CONF_PV_POWER, 0),
            "current_price_dkk_per_kwh": price,
            "expected_savings_dkk": self.plan.expected_savings_dkk
            if self.plan
            else None,
            "expected_cost_dkk": self.plan.expected_cost_dkk if self.plan else None,
            "baseline_cost_dkk": self.plan.baseline_cost_dkk if self.plan else None,
            "planned_battery_throughput_kwh": self.plan.battery_throughput_kwh
            if self.plan
            else None,
            "terminal_price_dkk_per_kwh": self.plan.terminal_price_dkk_per_kwh
            if self.plan
            else None,
            "effective_target_soc": effective_settings.target_soc
            if effective_settings
            else None,
            "price_spread_dkk_per_kwh": storage_policy.price_spread_dkk_per_kwh
            if storage_policy
            else None,
            "effective_margin_dkk_per_kwh": storage_policy.effective_margin_dkk_per_kwh
            if storage_policy
            else None,
            "extra_storage_active": storage_policy.active if storage_policy else False,
            "extra_storage_reason": storage_policy.reason if storage_policy else None,
            "plan_created_at": self.plan.created_at.isoformat() if self.plan else None,
            "plan": self.plan.as_dict() if self.plan else None,
            "today": today,
            "month": month,
            "lifetime_charge_kwh": self.runtime.ledger.total_charge_kwh,
            "lifetime_discharge_kwh": self.runtime.ledger.total_discharge_kwh,
            "lifetime_net_savings_dkk": self.runtime.ledger.total_net_savings_dkk,
            "equivalent_full_cycles": equivalent_cycles,
            "estimated_degradation_pct": degradation_pct,
            "estimated_remaining_capacity_pct": 100 - degradation_pct,
            "learned_capacity_kwh": (
                self.runtime.battery_learner.learned_capacity_wh / 1000
                if self.runtime.battery_learner.learned_capacity_wh
                else None
            ),
            "learned_efficiency_pct": (
                self.runtime.battery_learner.learned_efficiency * 100
                if self.runtime.battery_learner.learned_efficiency
                else None
            ),
            "load_learning_confidence_pct": self.runtime.load_learner.confidence * 100,
            "load_forecast_error_w": self.runtime.load_learner.mean_absolute_error_w,
            "learning_observations": self.runtime.load_learner.observations,
            "price_forecast_enabled": self.runtime.forecast_enabled,
            "price_forecast_source": self._forecast_source,
            "price_forecast_status": self._forecast_status,
            "price_forecast_last_updated": self._forecast_last_updated,
            "price_forecast_available_slots": self._forecast_slot_count,
            "price_forecast_used_slots": self._forecast_used_slot_count,
            "price_forecast_accuracy": self.runtime.forecast_accuracy.quality,
            "price_forecast_samples": self.runtime.forecast_accuracy.samples,
            "price_forecast_mae_dkk_per_kwh": (
                self.runtime.forecast_accuracy.mean_absolute_error_dkk_per_kwh
            ),
            "price_forecast_bias_dkk_per_kwh": (
                self.runtime.forecast_accuracy.mean_bias_dkk_per_kwh
            ),
            "price_forecast_within_uncertainty_pct": (
                self.runtime.forecast_accuracy.within_uncertainty_pct
            ),
            "capacity_learning_samples": len(
                self.runtime.battery_learner.capacity_samples_wh
            ),
            "capacity_learning_ready": self.runtime.battery_learner.capacity_ready,
            "efficiency_learning_samples": len(
                self.runtime.battery_learner.efficiency_samples
            ),
            "efficiency_learning_ready": self.runtime.battery_learner.efficiency_ready,
            "scheduled_load_count": len(self.runtime.scheduled_loads),
            "recent_decisions": self.runtime.decisions[-20:],
            "health_problems": problems,
            "last_refresh": now.isoformat(),
        }

    async def async_set_execution_enabled(self, enabled: bool) -> None:
        if enabled and not self.config.get(CONF_COMMISSIONED, False):
            raise ValueError(
                "Commission the integration in Options before enabling control"
            )
        if enabled and (
            problems := soc_control_problems(
                self.hass,
                self.config,
                absolute_min_soc=self.runtime.settings["absolute_min_soc"],
                maximum_soc=self.runtime.settings["opportunistic_target_soc"],
            )
        ):
            raise ValueError(
                "Automatic control requires commissioned native minimum and maximum "
                f"SOC controls: {'; '.join(problems)}"
            )
        was_enabled = self.runtime.execution_enabled
        self.runtime.execution_enabled = enabled
        await self.store.save(self.runtime)
        if was_enabled and not enabled:
            await self.actuator.async_command(Action.SAFE, datetime.now(UTC))
        await self.async_request_refresh()

    async def async_set_forecast_enabled(self, enabled: bool) -> None:
        """Opt in to planning beyond known prices with the configured forecast."""
        if enabled and not self.config.get(CONF_PRICE_FORECAST_ENTITY):
            raise ValueError("Configure an external price forecast entity first")
        self.runtime.forecast_enabled = enabled
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_set_setting(self, key: str, value: float) -> None:
        self.runtime.settings[key] = value
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_force_safe(self) -> None:
        self.runtime.execution_enabled = False
        await self.store.save(self.runtime)
        await self.actuator.async_command(Action.SAFE, datetime.now(UTC))
        await self.async_request_refresh()

    async def async_add_scheduled_load(
        self, start: datetime, end: datetime, additional_w: float, label: str
    ) -> None:
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("Scheduled load needs timezone-aware start < end")
        if not 0 < additional_w <= 20000:
            raise ValueError("additional_w must be in (0, 20000]")
        self.runtime.scheduled_loads.append(
            {
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
                "additional_w": additional_w,
                "label": label[:80],
            }
        )
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    def export_data(self) -> dict[str, Any]:
        return self.runtime.export(self.entry.title, self.data)
