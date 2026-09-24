"""Coordinator for learning, planning, accounting, and guarded execution."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .accounting import calendar_period_bounds
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
    FORECAST_MAX_AGE,
    LOCAL_TCP_RECOVERY_GRACE,
    MODE_BATTERY,
    MODE_CHARGE,
    MODE_GRID,
    MODE_SAFE,
    OVERRIDE_AUTO,
    OVERRIDE_OPTIONS,
    OVERRIDE_TO_ACTION,
    UPDATE_INTERVAL,
)
from .dailyplan import local_day_bounds, reconcile_daily_plan
from .evidence import EvidenceCollector
from .forecast import (
    assess_external_forecast,
    detect_extreme_price_movement,
    extend_known_horizon,
)
from .forecast_plan import build_forecast_plan
from .health import get_health_problems
from .learning import LoadLearner
from .local_tcp import (
    FbpLocalSnapshot,
    FbpLocalTcpClient,
    FbpTelemetryValidator,
    LocalProtocolError,
    operating_mode_from_controls,
)
from .models import (
    Action,
    Plan,
    PlannerSettings,
    PriceSlot,
    normalize_grid_flow,
)
from .planner import optimize
from .policy import (
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
        self._daily_plan_dirty = False
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
        # Coarse, non-detailed guideline over the *forecast* horizon only.
        # This is deliberately separate from ``self.plan``: the forecast is
        # uncertain and must never drive the optimizer, but it is useful
        # guidance for when to charge or use the battery after known prices.
        self._forecast_plan = None
        self._extreme_price_indicator: dict[str, float | bool] = {
            "is_extreme": False,
            "baseline": 0.0,
            "max_future": 0.0,
            "ratio": 0.0,
        }
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
        self._local_tcp_unavailable_since: datetime | None = None
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
            self._startup_control_gate_reason = "Awaiting fresh local telemetry and SOC-control validation after restart"
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
        known_rows: list[Any] = []
        forecast_price_entities: list[str] = []
        for entity_id in self.config.get(CONF_PRICE_ENTITIES, []):
            state = self.hass.states.get(entity_id)
            if not state:
                continue
            attributes = dict(state.attributes)
            if attributes.get("forecast_data"):
                # A price entity may flag its rows as a forecast rather than a
                # confirmed price.  Forecast data must never be optimised as if
                # it were known pricing, so route it through the forecast path
                # instead of the known-price set.
                forecast_price_entities.append(entity_id)
                continue
            known_rows.extend(extract_rows(attributes))
        known_slots = normalize_price_rows(known_rows)
        sources = self._forecast_entities()
        for extra in forecast_price_entities:
            if extra not in sources:
                sources.append(extra)
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
                    maximum_age=FORECAST_MAX_AGE,
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
        # Use known prices only for planning — forecasts are indicators only.
        # The price feed provides confirmed data up to ~36 hours ahead;
        # forecast slots carry uncertainty buffers that make the optimizer
        # unnecessarily conservative for the very spikes we want to react to.
        slots = known_slots
        self._forecast_planning_source = None
        self._forecast_used_slot_count = 0
        if self.runtime.forecast_enabled:
            # Detect extreme price movements using available forecasts.
            # Only flag when future prices deviate sharply from the recent
            # known-price baseline (default 2× the average).
            all_planning_slots = sorted(
                known_slots,
                key=lambda item: item.start,
            )
            # Add forecast slots to the detection set (they're just for
            # indicators, not for the optimizer itself).
            for source, forecast_slots in available_forecasts:
                all_planning_slots.extend(forecast_slots)
            self._extreme_price_indicator = detect_extreme_price_movement(
                all_planning_slots
            )

        for source, details in source_data.items():
            if details["status"] != "available":
                continue
            extension_slots = sum(
                slot.source == "forecast" for slot in extensions.get(source, [])
            )
            if not self.runtime.forecast_enabled:
                details["status"] = "disabled"
            elif extension_slots:
                details["status"] = "used_as_indicator"
            else:
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
        self._forecast_source = sources[0] if sources else None
        primary = source_data.get(self._forecast_source or "", {})
        self._forecast_status = primary.get("status", "not_configured")
        self._forecast_last_updated = primary.get("last_updated")
        self._forecast_slot_count = sum(
            int(details["available_slots"]) for details in source_data.values()
        )
        self._extreme_price_ratio = self._extreme_price_indicator.get("ratio", 0.0)
        self._extreme_price_is_extreme = self._extreme_price_indicator.get(
            "is_extreme", False
        )
        # Build the coarse forecast-only guideline.  All available forecast
        # sources are merged so the guideline reflects the whole forecast
        # horizon; contiguity gaps between sources are handled inside the
        # builder.  It never feeds the optimizer (``slots``/``known_slots``).
        all_forecast_slots: list[PriceSlot] = []
        for _, forecast_slots in available_forecasts:
            all_forecast_slots.extend(forecast_slots)
        self._forecast_plan = build_forecast_plan(
            all_forecast_slots,
            now=now,
            known_slots=known_slots,
        )
        result: list[PriceSlot] = []
        for slot in slots:
            if slot.end <= now:
                continue
            load_w = self.runtime.load_learner.predict_w(
                slot.start, self._load_power(0) or 0, now=now
            )
            scheduled_wh = self._scheduled_load_wh(slot.start, slot.end)
            # Known prices go to the optimizer with their full value.
            # Forecast slots are excluded from the planning horizon
            # because they carry uncertainty buffers that suppress
            # otherwise profitable arbitrage on extreme price spikes.
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

    def _override_action(self) -> Action | None:
        """Return the planner action forced by a manual override, if any.

        ``auto`` (the default) returns ``None`` so the optimizer plan decides.
        A forced mode returns the corresponding action so the coordinator
        commands it every refresh, independent of price and the plan. The
        SOC ceiling/floor applied by the actuator still bound the action:
        ``charge`` never raises above ``target_soc`` and ``battery`` never
        drops below ``reserve_soc``.
        """
        action_value = OVERRIDE_TO_ACTION.get(self.runtime.override_action)
        return Action(action_value) if action_value else None

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
        local_tcp_recovery_remaining: timedelta | None = None
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
                self._local_tcp_unavailable_since = None
            except LocalProtocolError as exc:
                self._local_snapshot = None
                self._local_controls = {}
                self._observed_local_mode = None
                local_problem = f"battery local TCP unavailable: {exc}"
                if self._local_tcp_unavailable_since is None:
                    self._local_tcp_unavailable_since = now
                elapsed = now - self._local_tcp_unavailable_since
                if elapsed < LOCAL_TCP_RECOVERY_GRACE:
                    local_tcp_recovery_remaining = LOCAL_TCP_RECOVERY_GRACE - elapsed
        problems = get_health_problems(self.hass, self.config, now)
        local_tcp_recovering = local_tcp_recovery_remaining is not None
        if local_problem and not local_tcp_recovering:
            problems.append(local_problem)
        if (
            direct_load_problem := self._direct_load_problem()
        ) and not local_tcp_recovering:
            problems.append(direct_load_problem)
        direct_control_problems: list[str] = []
        if (
            self.runtime.execution_enabled
            and self.is_direct_local
            and not local_tcp_recovering
        ):
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
        startup_waiting = (
            self._startup_control_gate_reason is not None
            and not local_tcp_recovering
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
        elif local_tcp_recovering:
            self.plan = None
            state = "RECOVERING"
            remaining_seconds = math.ceil(local_tcp_recovery_remaining.total_seconds())
            reason = (
                "Battery local TCP is temporarily unavailable; automatic writes are "
                f"paused for up to {LOCAL_TCP_RECOVERY_GRACE.seconds // 60} minutes "
                f"({remaining_seconds} seconds remaining): {local_problem}"
            )
            command_result = (
                "automatic writes paused during local TCP recovery grace period"
            )
        elif problems:
            state = "DEGRADED"
            reason = "; ".join(problems)
            command_result = await self._force_safe_if_needed(now)
        elif soc is not None:
            try:
                settings = self._settings()
                settings = apply_storage_policy(settings)
                self.plan = optimize(
                    slots,
                    now=now,
                    soc=soc,
                    settings=settings,
                    current_action=action if action is not Action.SAFE else Action.GRID,
                    mode_lock_remaining_minutes=self.runtime.mode_lock_remaining(now),
                    transition_times=self.runtime.active_transition_times(now),
                )
                effective_settings = settings
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
                # Re-anchor the whole day onto actual telemetry: keep the
                # published past (before the replan cutoff) immutable and replace
                # only the future portion with this fresh optimization.  The
                # optimizer already starts its future SOC from the observed SOC
                # (see ``optimize``), so no predicted SOC is treated as authority.
                local_now = dt_util.as_local(now)
                day_start, _day_end = local_day_bounds(local_now)
                # Extend the persisted timeline through the whole known-price
                # horizon (today plus any following days whose prices are already
                # available) rather than only the current calendar day.  The
                # optimizer only ever plans the future, so the reconciler keeps
                # the immutable past and recomputes only what lies past ``now``.
                horizon_end = (
                    self.plan.slots[-1].end if self.plan.slots else day_start
                )
                self.runtime.daily_plan = reconcile_daily_plan(
                    self.runtime.daily_plan,
                    self.plan.slots,
                    cutoff=now,
                    day_start=day_start,
                    horizon_end=horizon_end,
                )
                self._daily_plan_dirty = True
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
                    # A manual override forces the commanded battery action
                    # regardless of the optimizer plan, so each physical function
                    # can be verified in isolation. `auto` follows the plan.
                    override_action = self._override_action()
                    requested_action = (
                        override_action
                        if override_action is not None
                        else self.plan.current_action
                    )
                    # Enforce zero export at the command boundary: pass the
                    # measured battery-served load so the execution layer clamps
                    # the discharge setpoint below the load and a fixed-power
                    # slot can never export when the house load dips below it.
                    command_result = (
                        await self.actuator.async_command(
                            requested_action,
                            now,
                            target_soc=effective_settings.target_soc,
                            load_w=self._load_power(0) or 0,
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
        if (
            decision_changed
            or self._forecast_evidence_changed
            or self._daily_plan_dirty
        ):
            # RuntimeStore writes atomically, so persisting the whole runtime
            # publishes the reconciled daily plan without risk of a partial
            # timeline corrupting the stored day.  Clear the flag afterwards so a
            # no-op refresh does not rewrite storage every minute.
            await self.store.save(self.runtime)
            self._daily_plan_dirty = False

        local_now = dt_util.as_local(now)
        periods = {
            name: self.runtime.ledger.totals_between(
                start.astimezone(UTC), end.astimezone(UTC)
            )
            for name, (start, end) in calendar_period_bounds(local_now).items()
        }
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
        reported_health_problems = list(problems)
        if local_tcp_recovering and local_problem:
            reported_health_problems.append(
                "Local TCP recovery in progress: " + local_problem
            )
        return {
            "system_state": state,
            "healthy": (
                not problems and not local_tcp_recovering and grid_available is True
            ),
            "reason": reason,
            "current_action": self.plan.current_action.value
            if self.plan and self.plan.slots
            else Action.SAFE.value,
            "observed_action": action.value,
            "execution_enabled": self.runtime.execution_enabled,
            "mode_override": self.runtime.override_action,
            "automatic_control_startup_gate": self._startup_control_gate_reason,
            "commissioned": bool(self.config.get(CONF_COMMISSIONED, False)),
            "grid_available": grid_available,
            "command_result": command_result,
            "soc": soc,
            "load_power_w": self._load_power(),
            # Decompose the signed grid meter into import/export here so export
            # is visible as a first-class quantity rather than clamped to zero.
            "grid_flow_power_w": self._float(CONF_GRID_IMPORT_POWER),
            "grid_import_power_w": normalize_grid_flow(
                self._float(CONF_GRID_IMPORT_POWER) or 0.0
            )[0],
            "grid_export_power_w": normalize_grid_flow(
                self._float(CONF_GRID_IMPORT_POWER) or 0.0
            )[1],
            "battery_charge_power_w": self._float(CONF_BATTERY_CHARGE_POWER, 0),
            "battery_discharge_power_w": self._float(CONF_BATTERY_DISCHARGE_POWER, 0),
            "local_connected": self._local_snapshot is not None,
            "local_tcp_recovery_started_at": (
                self._local_tcp_unavailable_since.isoformat()
                if local_tcp_recovering and self._local_tcp_unavailable_since
                else None
            ),
            "local_tcp_recovery_remaining_seconds": (
                math.ceil(local_tcp_recovery_remaining.total_seconds())
                if local_tcp_recovery_remaining
                else 0
            ),
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
            "plan_created_at": (
                self.plan.created_at.isoformat() if self.plan else None
            ),
            "plan": (self.plan.today_dict(local_now) if self.plan else None),
            # Persisted 00:00 -> 24:00 daily timeline (immutable past + fresh
            # future).  Shown even during bootstrap/degraded/restart so the
            # dashboard never loses the current day's plan.
            "daily_plan": self.runtime.daily_plan.view_dict(
                actual_soc=soc,
                actual_soc_at=local_now.isoformat(),
                terminal_price_dkk_per_kwh=(
                    self.plan.terminal_price_dkk_per_kwh if self.plan else None
                ),
            ),
            **periods,
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
            # Extreme price indicator — forecasts are used only to detect
            # when prices deviate sharply from the recent baseline.
            "price_forecast_extreme_ratio": self._extreme_price_ratio,
            "price_forecast_is_extreme": self._extreme_price_is_extreme,
            # Coarse forecast-horizon guideline (separate from the real plan).
            "price_forecast_plan": (
                self._forecast_plan.as_dict() if self._forecast_plan else None
            ),
            "price_forecast_recommendation": (
                self._forecast_plan.summary() if self._forecast_plan else None
            ),
            "price_forecast_blocks": (
                len(self._forecast_plan.blocks) if self._forecast_plan else 0
            ),
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
            "health_problems": reported_health_problems,
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

    async def async_set_override_action(self, mode: str) -> None:
        """Set the manual operating override for the storage controller.

        ``auto`` (default) lets the optimizer plan decide the commanded action.
        A forced mode commands that action every refresh so the physical charge,
        discharge and grid functions can be tested independently of price. The
        override only affects writes; it never bypasses the SOC ceiling/floor:
        ``charge`` still stops at ``target_soc`` and ``battery`` still stops at
        ``reserve_soc``.
        """
        if mode not in OVERRIDE_OPTIONS:
            raise ValueError(f"Unsupported operating override: {mode}")
        if mode != OVERRIDE_AUTO and not self.config.get(CONF_COMMISSIONED, False):
            raise ValueError(
                "Commission the integration in Options before overriding control"
            )
        self.runtime.override_action = mode
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


def _control_number(controls: dict[str, str], key: str) -> float | None:
    try:
        value = float(controls[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None
