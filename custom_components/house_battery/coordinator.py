"""Coordinator for learning, planning, accounting, and guarded execution."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
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
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITIES,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_SOC,
    DECISION_HISTORY_LIMIT,
    DEFAULT_PORT,
    DIRECT_LOAD_FIELD,
    DOMAIN,
    MODE_BATTERY,
    MODE_CHARGE,
    MODE_GRID,
    MODE_SAFE,
    UPDATE_INTERVAL,
)
from .evidence import EvidenceCollector
from .forecast import assess_external_forecast, extend_known_horizon
from .health import get_health_problems
from .learning import LoadLearner
from .local_tcp import (
    FbpLocalSnapshot,
    FbpLocalTcpClient,
    FbpTelemetryValidator,
    LocalProtocolError,
    operating_mode_from_controls,
)
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
_PRICE_TOLERANCE_DKK_PER_KWH = 1e-9
_COST_TOLERANCE_DKK = 1e-9


def _matching_price_slot(
    planned_start: datetime, planned_end: datetime, slots: list[PriceSlot]
) -> PriceSlot | None:
    """Find the source interval backing a planned (possibly partial) slot."""
    return next(
        (
            slot
            for slot in slots
            if slot.start <= planned_start and planned_end <= slot.end
        ),
        None,
    )


def _strict_extra_storage_rejection(
    *,
    normal_plan: Plan,
    extra_plan: Plan,
    slots: list[PriceSlot],
    now: datetime,
    normal_target_soc: float,
    cheap_window_minutes: float,
) -> str | None:
    """Return why an extra-SOC plan is not a rare, valuable opportunity.

    The policy-level price-spread test merely makes an extra target eligible.
    This final gate compares executable plans and fails closed unless the extra
    energy is bought at the cheapest *known* price in a short opportunity.
    Forecast intervals cannot establish that opportunity.
    """
    if (
        extra_plan.expected_cost_dkk
        >= normal_plan.expected_cost_dkk - _COST_TOLERANCE_DKK
    ):
        return "Normal target retained; extra plan has no incremental expected-cost saving"

    extra_charge_slots: list[PriceSlot] = []
    for planned in extra_plan.slots:
        if planned.battery_charge_wh <= 0:
            continue
        # A charge that only reaches the normal target is not evidence for the
        # discretionary storage band.  SOC deltas are the executed energy
        # result after the planner's quantisation and losses.
        stored_above_normal_soc = max(
            0.0, planned.soc_end - max(planned.soc_start, normal_target_soc)
        )
        if stored_above_normal_soc <= _PRICE_TOLERANCE_DKK_PER_KWH:
            continue
        source_slot = _matching_price_slot(planned.start, planned.end, slots)
        if source_slot is None or source_slot.source != "known":
            return (
                "Normal target retained; extra charge is not in a known-price "
                "interval"
            )
        extra_charge_slots.append(source_slot)

    if not extra_charge_slots:
        return (
            "Normal target retained; extra plan does not charge above the normal target"
        )

    known_slots = [
        slot for slot in slots if slot.source == "known" and slot.end > now
    ]
    if not known_slots:
        return "Normal target retained; no known-price opportunity is available"
    cheapest_known_charge_price = min(
        slot.charge_price_dkk_per_kwh for slot in known_slots
    )
    for slot in extra_charge_slots:
        if (
            slot.charge_price_dkk_per_kwh
            > cheapest_known_charge_price + _PRICE_TOLERANCE_DKK_PER_KWH
        ):
            return (
                "Normal target retained; extra charge is not at the cheapest "
                "known charge price"
            )

    # The full remaining duration of equally cheap *known* intervals is the
    # opportunity window.  If it is longer than the configured window, there
    # is no need to buy discretionary energy now: an equally cheap opportunity
    # remains available for too long.  Forecast slots are deliberately absent.
    opportunity_minutes = sum(
        max(0.0, (slot.end - max(slot.start, now)).total_seconds() / 60)
        for slot in known_slots
        if abs(slot.charge_price_dkk_per_kwh - cheapest_known_charge_price)
        <= _PRICE_TOLERANCE_DKK_PER_KWH
    )
    if opportunity_minutes > cheap_window_minutes + _PRICE_TOLERANCE_DKK_PER_KWH:
        return (
            "Normal target retained; cheapest known-price opportunity lasts "
            f"{opportunity_minutes:g} minutes, above the "
            f"{cheap_window_minutes:g}-minute extra-storage limit"
        )
    return None


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
        self._forecast_sources: dict[str, dict[str, Any]] = {}
        self._forecast_planning_source: str | None = None
        self.local_client = (
            FbpLocalTcpClient(
                entry.data[CONF_HOST], int(entry.data.get("port", DEFAULT_PORT))
            )
            if entry.data.get(CONF_HOST)
            else None
        )
        self._local_snapshot: FbpLocalSnapshot | None = None
        self._local_controls: dict[str, str] = {}
        self._local_telemetry_validator = FbpTelemetryValidator()
        self._commanded_local_mode = MODE_SAFE
        self._observed_local_mode: str | None = None
        self._startup_control_gate_reason: str | None = None
        self.actuator = LocalControlAdapter(
            hass,
            lambda: self.config,
            lambda: self.runtime,
            lambda: self.store.save(self.runtime),
            lambda: self.local_client,
            self._set_commanded_local_mode,
        )
        self.evidence = EvidenceCollector(
            lambda: self.runtime,
            self._observed_power,
            lambda: self.store.save(self.runtime),
        )

    @property
    def config(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def is_direct_local(self) -> bool:
        return self.local_client is not None

    def _set_commanded_local_mode(self, mode: str) -> None:
        self._commanded_local_mode = mode

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            configuration_url="https://github.com/madslundt/House-battery",
        )

    async def async_initialize(self) -> None:
        self.runtime = await self.store.load()
        self.runtime.load_learner.configure_time_zone(
            dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        )
        if self.is_direct_local:
            learner_source = "direct:off_grid_total"
            if self.runtime.load_learner_source != learner_source:
                self.runtime.load_learner = LoadLearner()
                self.runtime.load_learner.configure_time_zone(
                    dt_util.get_time_zone(self.hass.config.time_zone) or UTC
                )
                self.runtime.load_learner_source = learner_source
                await self.store.save(self.runtime)
        if self.runtime.execution_enabled and self.is_direct_local:
            # Do not revoke persisted consent merely because the battery has
            # not finished accepting its local TCP session during HA startup.
            # The first fresh refresh validates controls and sends no command.
            self._startup_control_gate_reason = (
                "Awaiting fresh local telemetry and SOC-control validation after restart"
            )
        elif self.runtime.execution_enabled:
            problems = await self.async_soc_control_problems()
            if not self.config.get(CONF_COMMISSIONED, False) or problems:
                self.runtime.execution_enabled = False
                await self.store.save(self.runtime)

    def _state(self, key: str):
        entity_id = self.config.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def _float(self, key: str, default: float | None = None) -> float | None:
        if self._local_snapshot is not None:
            local_values = {
                CONF_SOC: self._local_snapshot.soc,
                CONF_BATTERY_CHARGE_POWER: self._local_snapshot.charge_power_w,
                CONF_BATTERY_DISCHARGE_POWER: self._local_snapshot.discharge_power_w,
            }
            if key in local_values:
                return local_values[key]
        state = self._state(key)
        try:
            return (
                float(state.state)
                if state and state.state.lower() not in _BAD_STATES
                else default
            )
        except (TypeError, ValueError):
            return default

    def _load_power(self, default: float | None = None) -> float | None:
        """Return only a configured or explicitly confirmed served-load value."""
        configured = self._float(CONF_LOAD_POWER)
        if configured is not None and not self.is_direct_local:
            return configured
        if self.is_direct_local and self._local_snapshot is not None:
            diagnostics = self._local_snapshot.load_diagnostics
            value = diagnostics.get(DIRECT_LOAD_FIELD)
            if isinstance(value, (int, float)):
                return float(value)
        return default

    def _direct_load_problem(self) -> str | None:
        """Explain why direct-local load data is not safe for the model yet."""
        if not self.is_direct_local:
            return None
        if self._local_snapshot is None:
            return "battery-served off-grid load cannot be read without local telemetry"
        value = self._local_snapshot.load_diagnostics.get(DIRECT_LOAD_FIELD)
        if not isinstance(value, (int, float)):
            return "complete battery-served off-grid load is unavailable"
        return None

    def _direct_soc_control_problems_from_snapshot(self) -> list[str]:
        """Validate controls read with the fresh telemetry frame, without a race."""
        minimum = _control_number(self._local_controls, "3023")
        maximum = _control_number(self._local_controls, "3024")
        if minimum is None or maximum is None or not 0 <= minimum <= maximum <= 100:
            return ["native SOC controls report invalid or unavailable bounds"]
        requested_absolute_min = self.runtime.settings["absolute_min_soc"]
        requested_reserve = self.runtime.settings["reserve_soc"]
        requested_max = self.runtime.settings["opportunistic_target_soc"]
        if not 0 <= requested_absolute_min <= requested_reserve <= requested_max <= 100:
            return ["configured SOC limits are invalid"]
        return []

    def _observed_power(self, key: str, default: float | None = None) -> float | None:
        if key == CONF_LOAD_POWER:
            return self._load_power(default)
        return self._float(key, default)

    def _grid_available(self) -> bool | None:
        """Read physical grid availability; never infer it from grid import power."""
        state = self._state(CONF_GRID_AVAILABLE)
        return parse_grid_available(state.state if state else None)

    def _forecast_entities(self) -> list[str]:
        """Return configured forecast entities, preserving their priority order."""
        config = self.config
        raw_sources = config.get(CONF_PRICE_FORECAST_ENTITIES)
        if raw_sources is None:
            raw_sources = config.get(CONF_PRICE_FORECAST_ENTITY)
        if isinstance(raw_sources, str):
            candidates = [raw_sources]
        elif isinstance(raw_sources, (list, tuple)):
            candidates = raw_sources
        else:
            candidates = []
        sources: list[str] = []
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and candidate not in sources:
                sources.append(candidate)
        return sources

    def _price_slots(self, now: datetime) -> list[PriceSlot]:
        rows: list[Any] = []
        for entity_id in self.config.get(CONF_PRICE_ENTITIES, []):
            state = self.hass.states.get(entity_id)
            if state:
                rows.extend(extract_rows(dict(state.attributes)))
        known_slots = normalize_price_rows(rows)
        sources = self._forecast_entities()
        for source in sources:
            self.runtime.migrate_legacy_forecast_accuracy(source)

        uncertainty = self.runtime.settings["forecast_uncertainty_dkk_per_kwh"]
        source_data: dict[str, dict[str, Any]] = {}
        available_forecasts: list[tuple[str, list[PriceSlot]]] = []
        for source in sources:
            forecast_state = self.hass.states.get(source)
            details: dict[str, Any] = {
                "status": "unavailable",
                "last_updated": None,
                "available_slots": 0,
                "used_slots": 0,
            }
            if (
                forecast_state is not None
                and forecast_state.state.lower() not in _BAD_STATES
            ):
                reported_at = (
                    getattr(forecast_state, "last_reported", None)
                    or forecast_state.last_updated
                )
                if reported_at is not None:
                    details["last_updated"] = reported_at.isoformat()
                assessment = assess_external_forecast(
                    extract_rows(dict(forecast_state.attributes)),
                    now=now,
                    reported_at=reported_at,
                    maximum_age=timedelta(
                        minutes=self.runtime.settings["forecast_max_age_minutes"]
                    ),
                )
                details["status"] = assessment.status
                forecast_slots = list(assessment.slots)
                details["available_slots"] = len(forecast_slots)
                if forecast_slots:
                    available_forecasts.append((source, forecast_slots))
            source_data[source] = details

        before = {
            source: accuracy.as_dict()
            for source, accuracy in self.runtime.forecast_accuracies.items()
        }
        # Score every outstanding prediction even when the source has gone
        # stale; actual prices arriving later must still close its comparison.
        for accuracy in self.runtime.forecast_accuracies.values():
            accuracy.score_actual_prices(
                known_slots, uncertainty_dkk_per_kwh=uncertainty
            )
        for source, forecast_slots in available_forecasts:
            accuracy = self.runtime.forecast_accuracy_for(source)
            accuracy.record_forecasts(forecast_slots)
            accuracy.score_actual_prices(
                known_slots, uncertainty_dkk_per_kwh=uncertainty
            )
        self._forecast_evidence_changed = before != {
            source: accuracy.as_dict()
            for source, accuracy in self.runtime.forecast_accuracies.items()
        }

        extensions = {
            source: extend_known_horizon(
                known_slots, forecast_slots, uncertainty_dkk_per_kwh=uncertainty
            )
            for source, forecast_slots in available_forecasts
        }
        slots = known_slots
        self._forecast_planning_source = None
        self._forecast_used_slot_count = 0
        if self.runtime.forecast_enabled:
            for source, _forecast_slots in available_forecasts:
                extension = extensions[source]
                used_slots = sum(slot.source == "forecast" for slot in extension)
                if used_slots:
                    slots = extension
                    self._forecast_planning_source = source
                    self._forecast_used_slot_count = used_slots
                    source_data[source]["used_slots"] = used_slots
                    break

        for source, details in source_data.items():
            if details["status"] != "available":
                continue
            extension_slots = sum(
                slot.source == "forecast" for slot in extensions.get(source, [])
            )
            if not self.runtime.forecast_enabled:
                details["status"] = "disabled"
            elif source == self._forecast_planning_source:
                details["status"] = "used"
            elif not extension_slots:
                details["status"] = "no_contiguous_extension"

        for source, details in source_data.items():
            accuracy = self.runtime.forecast_accuracy_for(source)
            details["accuracy"] = {
                "quality": accuracy.quality,
                "samples": accuracy.samples,
                "mae_dkk_per_kwh": accuracy.mean_absolute_error_dkk_per_kwh,
                "bias_dkk_per_kwh": accuracy.mean_bias_dkk_per_kwh,
                "within_uncertainty_pct": accuracy.within_uncertainty_pct,
            }
        self._forecast_sources = source_data
        self._forecast_source = self._forecast_planning_source or (
            sources[0] if sources else None
        )
        primary = source_data.get(self._forecast_source or "", {})
        self._forecast_status = primary.get("status", "not_configured")
        self._forecast_last_updated = primary.get("last_updated")
        self._forecast_slot_count = sum(
            int(details["available_slots"]) for details in source_data.values()
        )
        result: list[PriceSlot] = []
        for slot in slots:
            if slot.end <= now:
                continue
            load_w = self.runtime.load_learner.predict_w(
                slot.start, self._load_power(0) or 0
            )
            scheduled_wh = self._scheduled_load_wh(slot.start, slot.end)
            result.append(
                PriceSlot(
                    slot.start,
                    slot.end,
                    slot.price,
                    expected_load_wh=load_w * slot.hours + scheduled_wh,
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
        if self.is_direct_local:
            return action_from_operating_mode(
                self._observed_local_mode or self._commanded_local_mode
            )
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
        local_problem: str | None = None
        if self.local_client is not None:
            try:
                snapshot = await self.local_client.async_snapshot()
                if problem := self._local_telemetry_validator.validate(snapshot, now):
                    self._local_snapshot = None
                    local_problem = f"battery local TCP telemetry rejected: {problem}"
                else:
                    self._local_snapshot = snapshot
                self._local_controls = await self.local_client.async_read_controls()
                self._observed_local_mode = operating_mode_from_controls(
                    self._local_controls
                )
            except LocalProtocolError as exc:
                self._local_snapshot = None
                self._local_controls = {}
                self._observed_local_mode = None
                local_problem = f"battery local TCP unavailable: {exc}"
        problems = get_health_problems(self.hass, self.config, now)
        if local_problem:
            problems.append(local_problem)
        if direct_load_problem := self._direct_load_problem():
            problems.append(direct_load_problem)
        direct_control_problems: list[str] = []
        if self.runtime.execution_enabled and self.is_direct_local:
            direct_control_problems = self._direct_soc_control_problems_from_snapshot()
            problems.extend(direct_control_problems)
        soc = self._float(CONF_SOC)
        slots = self._price_slots(now)
        action = self._observed_action()
        grid_available = self._grid_available()
        price = self._current_price(slots, now)
        if soc is not None and not direct_load_problem:
            await self.evidence.async_observe(now, price, action, soc)

        state = "BOOTSTRAP"
        reason = "Waiting for valid local telemetry and price intervals"
        command_result = "no command"
        effective_settings: PlannerSettings | None = None
        storage_policy: StoragePolicy | None = None
        startup_waiting = (
            self._startup_control_gate_reason is not None
            and direct_load_problem is None
            and (bool(problems) or grid_available is not True)
        )
        if startup_waiting:
            self.plan = None
            state = "BOOTSTRAP"
            reason = (
                "Awaiting fresh post-restart telemetry and control validation: "
                + "; ".join(problems or ["grid availability is not confirmed"])
            )
            command_result = "startup gate is inhibiting writes"
        elif grid_available is False:
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
                normal_settings = self._settings()
                candidate_settings, storage_policy = apply_storage_policy(
                    normal_settings,
                    slots,
                    extra_storage_spread_dkk_per_kwh=self.runtime.settings[
                        "extra_storage_spread_dkk_per_kwh"
                    ],
                    opportunistic_target_soc=self.runtime.settings[
                        "opportunistic_target_soc"
                    ],
                )
                normal_plan = optimize(
                    slots,
                    now=now,
                    soc=soc,
                    settings=normal_settings,
                    current_action=action if action is not Action.SAFE else Action.GRID,
                    mode_lock_remaining_minutes=self.runtime.mode_lock_remaining(now),
                    transitions_used=self.runtime.transitions_used(now),
                )
                effective_settings = normal_settings
                self.plan = normal_plan
                if storage_policy.active:
                    extra_plan = optimize(
                        slots,
                        now=now,
                        soc=soc,
                        settings=candidate_settings,
                        current_action=(
                            action if action is not Action.SAFE else Action.GRID
                        ),
                        mode_lock_remaining_minutes=self.runtime.mode_lock_remaining(
                            now
                        ),
                        transitions_used=self.runtime.transitions_used(now),
                    )
                    rejection = _strict_extra_storage_rejection(
                        normal_plan=normal_plan,
                        extra_plan=extra_plan,
                        slots=slots,
                        now=now,
                        normal_target_soc=normal_settings.target_soc,
                        cheap_window_minutes=self.runtime.settings[
                            "extra_storage_cheap_window_minutes"
                        ],
                    )
                    if rejection is None:
                        effective_settings = candidate_settings
                        self.plan = extra_plan
                    else:
                        storage_policy = replace(
                            storage_policy,
                            target_soc=normal_settings.target_soc,
                            active=False,
                            reason=rejection,
                        )
            except ValueError as exc:
                self.plan = None
                problems.append(str(exc))
            if not self.plan or not self.plan.slots:
                if self._startup_control_gate_reason is not None:
                    state = "BOOTSTRAP"
                    reason = (
                        "Awaiting a valid post-restart plan before allowing "
                        "automatic writes: "
                        + (self.plan.reason if self.plan else "; ".join(problems))
                    )
                    command_result = "startup gate is inhibiting writes"
                else:
                    state = "DEGRADED"
                    reason = self.plan.reason if self.plan else "; ".join(problems)
                    command_result = await self._force_safe_if_needed(now)
            else:
                self._startup_control_gate_reason = None
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
        primary_accuracy = (
            self.runtime.forecast_accuracies.get(self._forecast_source)
            if self._forecast_source
            else None
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
            "automatic_control_startup_gate": self._startup_control_gate_reason,
            "commissioned": bool(self.config.get(CONF_COMMISSIONED, False)),
            "grid_available": grid_available,
            "command_result": command_result,
            "soc": soc,
            "load_power_w": self._load_power(),
            "grid_import_power_w": self._float(CONF_GRID_IMPORT_POWER),
            "battery_charge_power_w": self._float(CONF_BATTERY_CHARGE_POWER, 0),
            "battery_discharge_power_w": self._float(CONF_BATTERY_DISCHARGE_POWER, 0),
            "local_connected": self._local_snapshot is not None,
            "local_load_diagnostics": (
                self._local_snapshot.load_diagnostics
                if self._local_snapshot is not None
                else None
            ),
            "native_min_soc": _control_number(self._local_controls, "3023"),
            "native_max_soc": _control_number(self._local_controls, "3024"),
            "local_operating_mode": self._commanded_local_mode
            if self.is_direct_local
            else None,
            "local_observed_mode": self._observed_local_mode
            if self.is_direct_local
            else None,
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
            "extra_storage_known_slot_count": storage_policy.known_slot_count
            if storage_policy
            else None,
            "extra_storage_charge_price_dkk_per_kwh": (
                storage_policy.conservative_charge_price_dkk_per_kwh
                if storage_policy
                else None
            ),
            "extra_storage_discharge_price_dkk_per_kwh": (
                storage_policy.conservative_discharge_price_dkk_per_kwh
                if storage_policy
                else None
            ),
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
            "price_forecast_planning_source": self._forecast_planning_source,
            "price_forecast_sources": self._forecast_sources,
            "price_forecast_status": self._forecast_status,
            "price_forecast_last_updated": self._forecast_last_updated,
            "price_forecast_available_slots": self._forecast_slot_count,
            "price_forecast_used_slots": self._forecast_used_slot_count,
            "price_forecast_accuracy": (
                primary_accuracy.quality if primary_accuracy else "unknown"
            ),
            "price_forecast_samples": (
                primary_accuracy.samples if primary_accuracy else 0
            ),
            "price_forecast_mae_dkk_per_kwh": (
                primary_accuracy.mean_absolute_error_dkk_per_kwh
                if primary_accuracy
                else None
            ),
            "price_forecast_bias_dkk_per_kwh": (
                primary_accuracy.mean_bias_dkk_per_kwh if primary_accuracy else None
            ),
            "price_forecast_within_uncertainty_pct": (
                primary_accuracy.within_uncertainty_pct if primary_accuracy else None
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
        if enabled and (problems := await self.async_soc_control_problems()):
            raise ValueError(
                "Automatic control requires commissioned native minimum and maximum "
                f"SOC controls: {'; '.join(problems)}"
            )
        if enabled and self.is_direct_local:
            if problem := self._direct_load_problem():
                raise ValueError(
                    "Automatic control requires complete battery-served off-grid "
                    f"load: {problem}"
                )
            self.runtime.collapse_rapid_transition_burst(
                datetime.now(UTC),
                maximum_transitions=round(
                    self.runtime.settings["maximum_transitions_per_day"]
                ),
            )
        was_enabled = self.runtime.execution_enabled
        self.runtime.execution_enabled = enabled
        self._startup_control_gate_reason = None
        await self.store.save(self.runtime)
        if was_enabled and not enabled:
            await self.actuator.async_command(Action.SAFE, datetime.now(UTC))
        await self.async_request_refresh()

    async def async_set_forecast_enabled(self, enabled: bool) -> None:
        """Opt in to planning beyond known prices with the configured forecast."""
        if enabled and not self._forecast_entities():
            raise ValueError(
                "Configure at least one external price forecast entity first"
            )
        self.runtime.forecast_enabled = enabled
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_set_setting(self, key: str, value: float) -> None:
        self.runtime.settings[key] = value
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_soc_control_problems(self) -> list[str]:
        """Validate direct controls or legacy bound controls before writes."""
        if not self.is_direct_local:
            return soc_control_problems(
                self.hass,
                self.config,
                absolute_min_soc=self.runtime.settings["absolute_min_soc"],
                reserve_soc=self.runtime.settings["reserve_soc"],
                maximum_soc=self.runtime.settings["opportunistic_target_soc"],
            )
        try:
            assert self.local_client is not None
            controls = await self.local_client.async_read_controls()
            minimum = _control_number(controls, "3023")
            maximum = _control_number(controls, "3024")
        except (AssertionError, LocalProtocolError):
            return ["native SOC controls are unavailable over local TCP"]
        if minimum is None or maximum is None or not 0 <= minimum <= maximum <= 100:
            return ["native SOC controls report invalid bounds"]
        requested_absolute_min = self.runtime.settings["absolute_min_soc"]
        requested_reserve = self.runtime.settings["reserve_soc"]
        requested_max = self.runtime.settings["opportunistic_target_soc"]
        if not 0 <= requested_absolute_min <= requested_reserve <= requested_max <= 100:
            return ["configured SOC limits are invalid"]
        self._local_controls = controls
        return []

    async def async_set_native_soc_limit(self, key: str, value: float) -> None:
        if not self.local_client:
            raise ValueError("This entity is only available for direct local setup")
        controls = await self.local_client.async_read_controls()
        minimum = round(_control_number(controls, "3023") or 0)
        maximum = round(_control_number(controls, "3024") or 100)
        if key == "minimum":
            minimum = round(value)
        elif key == "maximum":
            maximum = round(value)
        else:
            raise ValueError(f"Unknown native SOC limit: {key}")
        await self.local_client.async_set_limits(minimum, maximum)
        self._local_controls = {"3023": str(minimum), "3024": str(maximum)}
        await self.async_request_refresh()

    async def async_set_manual_mode(self, mode: str) -> None:
        if not self.local_client:
            raise ValueError("This entity is only available for direct local setup")
        controls = await self.local_client.async_read_controls()
        minimum = round(_control_number(controls, "3023") or 0)
        maximum = round(_control_number(controls, "3024") or 100)
        if mode == MODE_BATTERY:
            await self.local_client.async_set_self_consumption()
        elif mode == MODE_CHARGE:
            await self.local_client.async_set_mode(
                "Charge",
                round(self.runtime.settings["charge_power_w"]),
                min_soc=minimum,
                max_soc=maximum,
            )
        elif mode == MODE_GRID:
            await self.local_client.async_set_mode(
                "Idle", 0, min_soc=minimum, max_soc=maximum
            )
        else:
            raise ValueError(f"Unsupported local operating mode: {mode}")
        self._local_controls = controls
        self._set_commanded_local_mode(mode)
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


def _control_number(controls: dict[str, str], key: str) -> float | None:
    try:
        value = float(controls[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None
