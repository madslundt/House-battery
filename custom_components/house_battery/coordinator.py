"""Coordinator for learning, planning, accounting, and guarded execution."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .accounting import calendar_period_bounds
from .actuator import LocalControlAdapter, soc_control_problems
from .adaptive import adaptive_policy, plan_economics
from .capabilities import ControlCapabilities
from .const import (
    CONF_ACCOUNTING_LOAD_POWER,
    CONF_CHARGE_POWER_CONTROL,
    CONF_COMMISSIONED,
    CONF_EFFICIENCY_GRID_POWER,
    CONF_DEVICE_TYPE,
    DEVICE_ECOFLOW,
    CONF_ECOFLOW_CAPACITY_KWH,
    CONF_ECOFLOW_DISCHARGE_POWER_W,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT_POWER,
    CONF_GRID_ISOLATION_SWITCH,
    CONF_LOAD_POWER,
    CONF_MAX_SOC_CONTROL,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITIES,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_SOC,
    DECISION_HISTORY_LIMIT,
    DEFAULT_PORT,
    DIRECT_LOAD_SOURCE,
    DOMAIN,
    EXPORT_SAFETY_W,
    FLOW_NOISE_FLOOR_W,
    FLOW_UI_ACTIVE_THRESHOLD_W,
    FORECAST_MAX_AGE,
    HIGH_SOC_DISCHARGE_DISCOUNT_DKK_PER_KWH,
    HIGH_SOC_DISCHARGE_THRESHOLD,
    LOCAL_TCP_RECOVERY_GRACE,
    LOW_SOC_CHARGE_PREMIUM_DKK_PER_KWH,
    LOW_SOC_CHARGE_THRESHOLD,
    MODE_BATTERY,
    MODE_CHARGE,
    MODE_GRID,
    MODE_SAFE,
    OPPORTUNISTIC_TARGET_SOC,
    OVERRIDE_AUTO,
    OVERRIDE_OPTIONS,
    OVERRIDE_TO_ACTION,
    RECOVERY_RELOAD_AFTER_FAILURES,
    RECOVERY_RELOAD_COOLDOWN,
    SWITCHING_PENALTY_DKK,
    UPDATE_INTERVAL,
)
from .dailyplan import local_day_bounds, reconcile_daily_plan
from .evidence import EvidenceCollector
from .ecoflow_capabilities import ecoflow_charge_power_range_w
from .forecast import (
    assess_external_forecast,
    detect_extreme_price_movement,
    extend_known_horizon,
    forecast_adaptation,
)
from .forecast_calibration import calibrate_forecast
from .forecast_plan import build_forecast_plan
from .health import fresh_numeric_value, get_health_problems
from .learning import LoadLearner
from .local_tcp import (
    FbpLocalSnapshot,
    FbpLocalTcpClient,
    FbpTelemetryValidator,
    LocalProtocolError,
    observed_battery_power,
    operating_mode_from_controls,
)
from .models import (
    Action,
    Plan,
    PlannedSlot,
    PlannerSettings,
    PowerFlowSnapshot,
    PriceSlot,
    derive_power_flow,
    estimate_battery_runtime_hours,
    normalize_grid_flow,
)
from .planner import future_price_slots, optimize
from .planning_evidence import (
    capture_evidence,
    evidence_scope,
    observe_actual_evidence,
    record_plan_evidence,
)
from .planning_schedule import (
    earliest_future_boundary,
    planning_change_reason,
    planning_key,
)
from .policy import (
    OpportunisticPlanDecision,
    action_from_operating_mode,
    apply_storage_policy,
    has_forecast_backed_movement,
    parse_grid_available,
    relative_storage_value,
    select_opportunistic_plan,
)
from .price import (
    diagnose_price_sources,
    extract_rows,
    is_forecast_data,
    normalize_price_rows,
)
from .runtime import RuntimeState, RuntimeStore
from .sizing import assess_sizing, sizing_prerequisites

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
        self._price_patterns_changed = False
        self._pattern_used_slots = 0
        self._pattern_tail_start: datetime | None = None
        self._pattern_supported_intervals: tuple[tuple[datetime, datetime], ...] = ()
        self._pattern_control_status = "not_available"
        self._pattern_control_reason: str | None = None
        self._economic_plan_key: tuple | None = None
        self._economic_decision: OpportunisticPlanDecision | None = None
        self._economic_plan_reused = False
        self._economic_plan_last_trigger: str | None = None
        self._economic_market_inputs: tuple[object, ...] | None = None
        self._price_tracking = False
        self._price_timer_cancel: Callable[[], None] | None = None
        self._price_timer_at: datetime | None = None
        self._last_price_slots: list[PriceSlot] = []
        self._forecast_slot_count = 0
        self._forecast_used_slot_count = 0
        self._forecast_source: str | None = None
        self._forecast_status = "not_configured"
        self._forecast_last_updated: str | None = None
        self._forecast_sources: dict[str, dict[str, Any]] = {}
        self._forecast_adaptation: dict[str, Any] = {}
        self._adaptation: dict[str, Any] = {}
        self._adaptation_dirty = False
        self._sizing_task: asyncio.Task | None = None
        self._sizing_request_key: str | None = None
        self._sizing_retry_after: datetime | None = None
        self._forecast_planning_source: str | None = None
        # Coarse, non-detailed guideline over the *forecast* horizon only.
        # This is deliberately separate from ``self.plan``. Only the qualified
        # contiguous extensions admitted by the forecast switch can enter the
        # executable optimizer; the coarse guideline also shows other sources.
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
        # Canonical load-vs-grid physical flow, rebuilt once per refresh and
        # shared by evidence, sensors and the export-safety layer.
        self.power_flow: PowerFlowSnapshot | None = None
        # Persistent export latch blocks inverter writes but preserves the
        # user's automatic-control authorization until they reset it.
        self._local_controls: dict[str, str] = {}
        self._local_telemetry_validator = FbpTelemetryValidator()
        self._local_tcp_unavailable_since: datetime | None = None
        self._consecutive_local_failures = 0
        self._consecutive_command_failures = 0
        self._recovery_reload_scheduled = False
        self._recovery_reload_reason: str | None = None
        self._commanded_local_mode = MODE_SAFE
        self._observed_local_mode: str | None = None
        self._startup_control_gate_reason: str | None = None
        self._planning_revision = 0
        self._safe_request_id = 0
        self._refresh_lock = asyncio.Lock()
        self._unloading = False
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
            self._power_flow,
            lambda: self.store.save(self.runtime),
            lambda: self._load_power(),
        )

    @property
    def config(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def is_direct_local(self) -> bool:
        return self.local_client is not None

    @property
    def battery_discharge_blocked(self) -> bool:
        """Retain the legacy diagnostic without blocking normal operation."""
        return False

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
        if (
            self.config.get(CONF_DEVICE_TYPE) == DEVICE_ECOFLOW
        ):
            ecoflow_settings_changed = False
            charge_entity = self.config.get("charge_power_control_entity")
            charge_state = self.hass.states.get(charge_entity) if charge_entity else None
            if charge_state is not None:
                try:
                    # Seed from EF BLE once; preserve later user choices while
                    # keeping them inside the current native device range.
                    power_min, power_max, power_step = ecoflow_charge_power_range_w(charge_state)
                    charge_power_w = (
                        power_max
                        if not self.runtime.ecoflow_setup_settings_applied
                        else min(max(self.runtime.settings["charge_power_w"], power_min), power_max)
                    )
                    charge_power_w = min(
                        max(
                            power_min
                            + round((charge_power_w - power_min) / power_step)
                            * power_step,
                            power_min,
                        ),
                        power_max,
                    )
                    if self.runtime.settings["charge_power_w"] != charge_power_w:
                        self.runtime.settings["charge_power_w"] = charge_power_w
                        ecoflow_settings_changed = True
                except ValueError:
                    _LOGGER.warning("EcoFlow charge power bounds are unavailable")
            if not self.runtime.ecoflow_setup_settings_applied:
                initial_settings = {
                    "capacity_kwh": self.config.get(CONF_ECOFLOW_CAPACITY_KWH),
                    "discharge_power_w": self.config.get(CONF_ECOFLOW_DISCHARGE_POWER_W),
                }
                if all(isinstance(value, (int, float)) for value in initial_settings.values()):
                    for key, value in initial_settings.items():
                        numeric_value = float(value)
                        if self.runtime.settings[key] != numeric_value:
                            self.runtime.settings[key] = numeric_value
                            ecoflow_settings_changed = True
                    self.runtime.ecoflow_setup_settings_applied = True
                    ecoflow_settings_changed = True
            if ecoflow_settings_changed:
                await self.store.save(self.runtime)
        if self.is_direct_local:
            learner_source = DIRECT_LOAD_SOURCE
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
            # Preserve the user's authorization across restarts and validation
            # failures. Health/commissioning gates still pause writes; valid
            # inputs let the coordinator resume without a manual switch cycle.
            problems = await self.async_soc_control_problems()
            if not self.config.get(CONF_COMMISSIONED, False) or problems:
                self._startup_control_gate_reason = (
                    "Waiting for commissioning and valid SOC controls before writes"
                )

    @callback
    def async_start_price_tracking(self) -> None:
        """Refresh on price publication and the actual provider's tariff boundaries."""
        if self._price_tracking:
            return
        self._price_tracking = True
        sources = list(dict.fromkeys([
            *self.config.get(CONF_PRICE_ENTITIES, []), *self._forecast_entities(),
        ]))
        if sources:
            self.entry.async_on_unload(async_track_state_change_event(
                self.hass, sources, self._async_price_changed,
            ))
        self.entry.async_on_unload(self._cancel_price_timer)
        self._schedule_price_refresh(self._last_price_slots, datetime.now(UTC))

    @callback
    def _async_price_changed(self, event: Event) -> None:
        if self._unloading:
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")

        def price_input(state: State | None) -> tuple | None:
            if state is None:
                return None
            attributes = dict(state.attributes)
            return (
                state.state.lower() in _BAD_STATES, is_forecast_data(attributes),
                extract_rows(attributes), attributes.get("unit_of_measurement"),
                attributes.get("currency"),
            )

        if price_input(old) == price_input(new):
            return
        self._planning_revision += 1
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _cancel_price_timer(self) -> None:
        if self._price_timer_cancel is not None:
            self._price_timer_cancel()
        self._price_timer_cancel = None
        self._price_timer_at = None

    @callback
    def _schedule_price_refresh(self, slots: list[PriceSlot], now: datetime) -> None:
        if not self._price_tracking or self._unloading:
            return
        boundaries = [earliest_future_boundary(slots, now)]
        for declaration in self.runtime.scheduled_loads:
            for key in ("start", "end"):
                value = dt_util.parse_datetime(str(declaration.get(key, "")))
                if value is not None and value.tzinfo is not None and value > now:
                    boundaries.append(value.astimezone(UTC))
        boundary = min((value for value in boundaries if value is not None), default=None)
        if boundary == self._price_timer_at:
            return
        self._cancel_price_timer()
        if boundary is None:
            return
        self._price_timer_at = boundary

        @callback
        def refresh(_when: datetime) -> None:
            self._price_timer_at = None
            self._price_timer_cancel = None
            if not self._unloading:
                self._planning_revision += 1
                self.hass.async_create_task(self.async_request_refresh())

        self._price_timer_cancel = async_track_point_in_utc_time(self.hass, refresh, boundary)

    def _sizing_source(self) -> str:
        return sha256(repr((
            DIRECT_LOAD_SOURCE if self.is_direct_local else self.config.get(CONF_LOAD_POWER),
            self.config.get(CONF_HOST) if self.is_direct_local else None,
            self.config.get("port", DEFAULT_PORT) if self.is_direct_local else None,
            tuple(self.config.get(CONF_PRICE_ENTITIES, [])),
        )).encode()).hexdigest()

    def _state(self, key: str):
        entity_id = self.config.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def _float(self, key: str, default: float | None = None) -> float | None:
        # For a direct-local entry, SOC and battery-served load come from the
        # TCP snapshot. Optional whole-house measurements are read from HA.
        if self.is_direct_local and self._local_snapshot is not None and key == CONF_SOC:
            return self._local_snapshot.soc
        value = fresh_numeric_value(self._state(key), datetime.now(UTC))
        return value if value is not None else default

    def _load_power(self, default: float | None = None) -> float | None:
        """Return only a configured or explicitly confirmed connected-load value.

        For a direct-local entry this is the complete per-storage off-grid
        total (``OffGridLoadPower`` summed across every ``Storage_list`` unit).
        It is the load behind the battery, which stays present in every mode.
        It is never the AI smart-load total (which can read 0 W) nor the
        device's reported discharge, which would wrongly assume the battery
        supplies the whole load. A partial frame fails closed to None.
        """
        if self.is_direct_local and self._local_snapshot is not None:
            value = self._local_snapshot.off_grid_load_total_w
            if isinstance(value, (int, float)) and math.isfinite(value):
                return float(value)
            return None
        return self._float(CONF_LOAD_POWER, default)

    def _direct_load_problem(self) -> str | None:
        """Explain why direct-local load data is not safe for the model yet."""
        if not self.is_direct_local:
            return None
        if self._local_snapshot is None:
            return "connected load cannot be read without local telemetry"
        if self._local_snapshot.off_grid_load_validation_error:
            return (
                "connected load telemetry is inconsistent: "
                + self._local_snapshot.off_grid_load_validation_error
            )
        if self._local_snapshot.off_grid_load_total_w is None:
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
        requested_max = OPPORTUNISTIC_TARGET_SOC
        if not 0 <= requested_absolute_min <= requested_reserve <= requested_max <= 100:
            return ["configured SOC limits are invalid"]
        return []

    def _observed_power(self, key: str, default: float | None = None) -> float | None:
        if key == CONF_LOAD_POWER:
            return self._load_power(default)
        return self._float(key, default)

    def _power_flow(self) -> PowerFlowSnapshot | None:
        """Build the canonical load-vs-grid flow for this refresh.

        Legacy HA-entity entries have measured load and grid inputs, so battery
        flow can be inferred from their balance. Direct-local entries only have
        a balanced household sample when independent whole-house load and grid
        meter entities are both configured.
        """
        now = datetime.now(UTC)
        # Balanced flow needs independent household measurements. The device's
        # MeterTotalActivePower is not a whole-house grid meter.
        if self.is_direct_local and (
            not self.config.get(CONF_GRID_IMPORT_POWER)
            or not self.config.get(CONF_ACCOUNTING_LOAD_POWER)
        ):
            return None
        soc = self._float(CONF_SOC)
        if soc is None:
            return None
        return derive_power_flow(
            soc,
            self._float(CONF_ACCOUNTING_LOAD_POWER)
            if self.is_direct_local
            else self._float(CONF_LOAD_POWER),
            self._float(CONF_GRID_IMPORT_POWER),
            timestamp=now,
        )

    def _classify_power_source(
        self, flow: PowerFlowSnapshot | None
    ) -> str | None:
        """Classify where the load's power is actually coming from.

        The ``power_source`` sensor is a read-only indicator of physical reality.
        For a legacy (HA-entity) entry the canonical load-vs-grid balance is the
        correct source. Direct-local entries classify from reported
        charge/discharge power so displayed activity stays independent from the
        requested mode. ``off`` means idle with no connected load; ``None``
        means the sample is unavailable.
        """
        if self.is_direct_local and self._local_snapshot is not None:
            threshold = FLOW_UI_ACTIVE_THRESHOLD_W
            charge, output = observed_battery_power(
                self._local_snapshot, self._local_controls
            )
            load = float(self._local_snapshot.off_grid_load_total_w or 0)
            if charge > threshold:
                return "charging"
            if output > threshold:
                return "battery"
            if load > threshold:
                return "grid"
            return "off"
        return flow.classify_power_source() if flow else None

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
        known_units: list[tuple[str, Any, Any]] = []
        forecast_price_entities: list[str] = []
        market_inputs: list[object] = []
        for entity_id in self.config.get(CONF_PRICE_ENTITIES, []):
            state = self.hass.states.get(entity_id)
            if not state:
                market_inputs.append((entity_id, "missing"))
                continue
            attributes = dict(state.attributes)
            market_inputs.append((
                entity_id, state.state.lower() in _BAD_STATES,
                is_forecast_data(attributes),
                dumps(extract_rows(attributes), sort_keys=True, default=str),
                attributes.get("unit_of_measurement"), attributes.get("currency"),
            ))
            if state.state.lower() not in _BAD_STATES:
                known_units.append((
                    entity_id, attributes.get("unit_of_measurement"), attributes.get("currency"),
                ))
            if is_forecast_data(attributes):
                # A price entity may flag its rows as a forecast rather than a
                # confirmed price.  Forecast data must never be optimised as if
                # it were known pricing, so route it through the forecast path
                # instead of the known-price set.
                forecast_price_entities.append(entity_id)
                continue
            known_rows.extend(extract_rows(attributes))
        known_slots = normalize_price_rows(known_rows)
        # Learn only elapsed, confirmed import prices in this installation's
        # units and tariff basis. Compatible retained history can warm-start it.
        # Keep missing feeds in the scope during temporary outages; metadata
        # changes on an available feed invalidate incompatible training units.
        previous_scope = self.runtime.price_patterns.source
        entities = list(self.config.get(CONF_PRICE_ENTITIES, []))
        try:
            previous_inputs = loads(previous_scope or "{}")
        except (JSONDecodeError, TypeError):
            previous_inputs = {}
        if not isinstance(previous_inputs, dict):
            previous_inputs = {}
        previous_units = previous_inputs.get("units", {})
        units = dict(previous_units) if (
            previous_inputs.get("entities") == entities and isinstance(previous_units, dict)
        ) else {}
        units.update({entity: [unit, currency] for entity, unit, currency in known_units})
        pattern_scope = dumps({"entities": entities, "units": units}, sort_keys=True)
        previous_pattern_timezone = self.runtime.price_patterns.time_zone
        self.runtime.price_patterns.configure(pattern_scope, self.hass.config.time_zone)
        if previous_scope is not None and previous_scope != pattern_scope:
            # A tariff-basis change must also invalidate seed history; otherwise
            # old units would immediately retrain the freshly cleared model.
            self.runtime.sizing_history.configure("")
        history = (
            self.runtime.sizing_history.recent_price_slots(now, days=90)
            if self.runtime.sizing_history.source == self._sizing_source() else []
        )
        history = [slot for slot in history if not any(
            actual.start < slot.end and actual.end > slot.start for actual in known_slots
        )]
        self._price_patterns_changed |= self.runtime.price_patterns.observe(
            [*history, *known_slots], now,
        ) or (
            previous_scope != pattern_scope
            or previous_pattern_timezone != self.hass.config.time_zone
        )
        # Keep the end of the forecast horizon stable within a tariff interval.
        # Otherwise a rolling 72-hour tail would invalidate the plan each minute.
        horizon_anchor = next((
            slot.tariff_start or slot.start for slot in known_slots
            if slot.start <= now < slot.end
        ), now)
        # Score independent candidates even when an external forecast fills
        # the horizon. Shadow evidence never authorizes learned control itself.
        self._price_patterns_changed |= self.runtime.price_patterns.shadow_issue(known_slots, now)
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
            market_inputs.append((
                source, details["status"],
                dumps(extract_rows(dict(forecast_state.attributes)), sort_keys=True, default=str)
                if forecast_state is not None else None,
                forecast_state.attributes.get("unit_of_measurement") if forecast_state else None,
                forecast_state.attributes.get("currency") if forecast_state else None,
            ))
        # Raw publications and availability are economic events. Model updates
        # below are deliberately excluded so learning cannot invalidate a plan
        # each minute while the provider's prices have stayed unchanged.
        self._economic_market_inputs = tuple(market_inputs)

        before = {
            source: accuracy.as_dict()
            for source, accuracy in self.runtime.forecast_accuracies.items()
        }
        # A configured price feed may contain forecast rows. Keep its evidence
        # during a temporary outage; only removing its binding retires it.
        self.runtime.prune(
            now,
            forecast_sources=set(sources) | set(self.config.get(CONF_PRICE_ENTITIES, [])),
        )
        # Score every outstanding prediction even when the source has gone
        # stale; actual prices arriving later must still close its comparison.
        for accuracy in self.runtime.forecast_accuracies.values():
            accuracy.score_actual_prices(
                known_slots, uncertainty_dkk_per_kwh=uncertainty, now=now,
            )
        known_end = max((slot.end for slot in known_slots), default=None)
        for source, forecast_slots in available_forecasts:
            accuracy = self.runtime.forecast_accuracy_for(source)
            # Published prices are no longer a prediction. In particular,
            # revised provider rows overlapping today's known prices must not
            # manufacture perfect evidence for automatic source selection.
            accuracy.record_forecasts(
                [slot for slot in forecast_slots
                 if known_end is None or slot.start >= known_end],
                now=now,
            )
            accuracy.score_actual_prices(
                known_slots, uncertainty_dkk_per_kwh=uncertainty, now=now,
            )
        self._forecast_evidence_changed = before != {
            source: accuracy.as_dict()
            for source, accuracy in self.runtime.forecast_accuracies.items()
        }

        self._forecast_adaptation = forecast_adaptation(
            [source for source in sources if any(
                available_source == source for available_source, _ in available_forecasts
            )],
            self.runtime.forecast_accuracies,
            now=now,
            configured_uncertainty=uncertainty,
            enabled=self.runtime.adaptive_enabled and self.runtime.forecast_enabled,
        )
        adaptive_sources = self._forecast_adaptation["sources"]
        planning_forecasts: list[tuple[str, list[PriceSlot]]] = []
        for source, forecast_slots in available_forecasts:
            effective_uncertainty = adaptive_sources[source]["effective_uncertainty_dkk_per_kwh"]
            calibrated, calibration = calibrate_forecast(
                [replace(slot, source="forecast", uncertainty_dkk_per_kwh=effective_uncertainty)
                 for slot in forecast_slots],
                self.runtime.forecast_accuracy_for(source), now=now,
                enabled=self.runtime.adaptive_enabled and self.runtime.forecast_enabled,
                uncertainty_floor=effective_uncertainty,
            )
            planning_forecasts.append((source, calibrated))
            source_data[source]["calibration"] = calibration
        extensions = {
            source: extend_known_horizon(
                known_slots,
                forecast_slots,
                now=horizon_anchor,
                uncertainty_dkk_per_kwh=adaptive_sources[source][
                    "effective_uncertainty_dkk_per_kwh"
                ],
            )
            for source, forecast_slots in planning_forecasts
        }
        slots = known_slots
        self._forecast_planning_source = None
        self._forecast_used_slot_count = 0
        if self.runtime.forecast_enabled:
            # The opt-in forecast switch extends, but never replaces or bridges,
            # known prices. Sources are tried in configured order and every
            # forecast slot carries its uncertainty buffer into the optimizer.
            for source in self._forecast_adaptation["source_order"]:
                extended = extensions.get(source, known_slots)
                forecast_count = sum(
                    slot.source == "forecast" for slot in extended
                )
                if forecast_count:
                    other_forecasts = [
                        slot
                        for other_source, other_slots in planning_forecasts
                        if other_source != source
                        for slot in other_slots
                    ]
                    # Conflicting feeds are additional forecast error, not a
                    # reason to silently trust whichever entity has priority.
                    slots = [
                        replace(
                            slot,
                            uncertainty_dkk_per_kwh=(
                                slot.uncertainty_dkk_per_kwh
                                + max(
                                    (
                                        abs(slot.price - other.price)
                                        for other in other_forecasts
                                        if other.start < slot.end
                                        and other.end > slot.start
                                    ),
                                    default=0.0,
                                )
                            ),
                        )
                        if slot.source == "forecast"
                        else slot
                        for slot in extended
                    ]
                    self._forecast_planning_source = source
                    self._forecast_used_slot_count = forecast_count
                    source_data[source]["used_slots"] = forecast_count
                    break
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
            elif source == self._forecast_planning_source:
                details["status"] = "used"
            elif extension_slots:
                details["status"] = "available"
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
        self._forecast_source = self._forecast_planning_source or (sources[0] if sources else None)
        primary = source_data.get(self._forecast_source or "", {})
        self._forecast_status = primary.get("status", "not_configured")
        self._forecast_last_updated = primary.get("last_updated")
        self._forecast_slot_count = sum(
            int(details["available_slots"]) for details in source_data.values()
        )
        prefix_length = len(slots)
        if self.runtime.forecast_enabled:
            slots = self.runtime.price_patterns.extend(
                slots, now, uncertainty, horizon_end=horizon_anchor + timedelta(hours=72),
            )
        pattern_slots = slots[prefix_length:]
        self._pattern_used_slots = len(pattern_slots)
        self._pattern_tail_start = pattern_slots[0].start if pattern_slots else None
        self._pattern_supported_intervals = tuple(
            (slot.start.astimezone(UTC), slot.end.astimezone(UTC)) for slot in pattern_slots
        )
        if pattern_slots:
            pattern_summary = self.runtime.price_patterns.summary(now)
            self._forecast_used_slot_count += len(pattern_slots)
            self._forecast_slot_count += len(pattern_slots)
            self._forecast_sources["learned_pattern"] = {
                "status": "used", "used_slots": len(pattern_slots),
                "available_slots": len(pattern_slots),
                "evidence": pattern_summary,
                "last_updated": pattern_summary["last_observation"],
            }
            if self._forecast_planning_source is None:
                self._forecast_planning_source = "learned_pattern"
                self._forecast_source = "learned_pattern"
                self._forecast_status = "used"
                self._forecast_last_updated = pattern_summary["last_observation"]
        self._extreme_price_ratio = self._extreme_price_indicator.get("ratio", 0.0)
        self._extreme_price_is_extreme = self._extreme_price_indicator.get(
            "is_extreme", False
        )
        # Build the coarse forecast-only guideline.  All available forecast
        # sources are merged so the guideline reflects the whole forecast
        # horizon; contiguity gaps between sources are handled inside the
        # builder. This is explanatory guidance; when forecast planning is
        # enabled, the selected contiguous extension also feeds the optimizer
        # separately through ``slots`` with its uncertainty buffer intact.
        all_forecast_slots: list[PriceSlot] = []
        for _, forecast_slots in available_forecasts:
            all_forecast_slots.extend(forecast_slots)
        all_forecast_slots.extend(pattern_slots)
        self._forecast_plan = build_forecast_plan(
            all_forecast_slots,
            now=now,
            known_slots=known_slots,
        )
        result: list[PriceSlot] = []
        # A missing/invalid connected load must fail closed, never fall back to
        # 0 W. Falling back to 0 would let the planner treat the battery as able
        # to serve an unquantified load and silently build a confident plan from
        # a phantom zero load; the caller already marks direct telemetry with a
        # missing off-grid total as DEGRADED. When a valid load is present it is
        # used immediately so the learner bootstraps without waiting for history.
        current_load = self._load_power()
        for slot in slots:
            if slot.end <= now:
                continue
            remaining_start = max(slot.start, now)
            load = self._forecast_load_wh(remaining_start, slot.end, now, current_load)
            if load is None:
                # Unknown load is not a zero-watt forecast.  Without a live
                # reading or sufficient learned profile, withhold the plan.
                return []
            baseline_wh, scheduled_wh = load
            # Known prices keep their full value. Forecast slots retain the
            # configured uncertainty so charging is priced conservatively high
            # and discharging conservatively low.
            result.append(
                PriceSlot(
                    remaining_start,
                    slot.end,
                    slot.price,
                    expected_load_wh=baseline_wh + scheduled_wh,
                    source=slot.source,
                    uncertainty_dkk_per_kwh=slot.uncertainty_dkk_per_kwh,
                    tariff_start=slot.tariff_start or slot.start,
                )
            )
        return result

    def _forecast_load_wh(
        self, start: datetime, end: datetime, now: datetime, current_load: float | None,
    ) -> tuple[float, float] | None:
        """Return independent baseline and declared incremental energy."""
        learner = self.runtime.load_learner
        fallback = learner.baseline_fallback(now, current_load, self.runtime.scheduled_loads)
        baseline = learner.predict_wh(start, end, fallback, now=now)
        if baseline is None:
            return None
        return baseline, self._scheduled_load_wh(start, end)

    def _scheduled_load_wh(self, start: datetime, end: datetime) -> float:
        total = 0.0
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        for item in self.runtime.scheduled_loads:
            try:
                item_start = datetime.fromisoformat(item["start"])
                item_end = datetime.fromisoformat(item["end"])
                watts = float(item["additional_w"])
                if (item_start.tzinfo is None or item_end.tzinfo is None
                        or not math.isfinite(watts) or not 0 < watts <= 20000):
                    continue
                item_start, item_end = item_start.astimezone(UTC), item_end.astimezone(UTC)
                overlap = max(
                    0.0, (min(end, item_end) - max(start, item_start)).total_seconds()
                )
                total += watts * overlap / 3600
            except (KeyError, TypeError, ValueError):
                continue
        return total

    def _settings(self) -> PlannerSettings:
        values = self.runtime.settings
        capacity = (
            self.runtime.battery_learner.learned_capacity_wh
            or values["capacity_kwh"] * 1000
        )
        efficiency, _ = self._planning_efficiency()
        charge_enabled = True
        if self.config.get(CONF_DEVICE_TYPE) == DEVICE_ECOFLOW:
            entity_id = self.config.get(CONF_CHARGE_POWER_CONTROL)
            state = self.hass.states.get(entity_id) if entity_id else None
            try:
                if state is None:
                    raise ValueError("AC Charging Speed is unavailable")
                ecoflow_charge_power_range_w(state)
            except ValueError:
                charge_enabled = False
        settings = PlannerSettings(
            capacity_wh=capacity,
            reserve_soc=values["reserve_soc"],
            target_soc=values["target_soc"],
            charge_power_w=values["charge_power_w"],
            discharge_power_w=values["discharge_power_w"],
            round_trip_efficiency=efficiency,
            degradation_cost_dkk_per_kwh=values["degradation_cost_dkk_per_kwh"],
            minimum_profit_dkk_per_kwh=values["minimum_profit_dkk_per_kwh"],
            switching_penalty_dkk=SWITCHING_PENALTY_DKK,
            low_soc_charge_threshold=LOW_SOC_CHARGE_THRESHOLD,
            low_soc_charge_premium_dkk_per_kwh=LOW_SOC_CHARGE_PREMIUM_DKK_PER_KWH,
            high_soc_discharge_threshold=HIGH_SOC_DISCHARGE_THRESHOLD,
            high_soc_discharge_discount_dkk_per_kwh=(
                HIGH_SOC_DISCHARGE_DISCOUNT_DKK_PER_KWH
            ),
            partial_charge_targets=self._partial_charge_targets(),
            charge_enabled=charge_enabled,
            control_capabilities=(
                ControlCapabilities.direct_local(
                    mains_isolated=bool(self.config.get(CONF_GRID_ISOLATION_SWITCH))
                ) if self.is_direct_local else ControlCapabilities()
            ),
        )
        decision = adaptive_policy(
            settings,
            enabled=self.runtime.adaptive_enabled,
            efficiency_summary=self._efficiency_summary(datetime.now(UTC)),
        )
        self._adaptation = decision.as_dict()
        self._record_adaptation(decision.settings)
        return decision.settings

    def _record_adaptation(self, settings: PlannerSettings) -> None:
        """Persist changed policy evidence, rather than every planning refresh."""
        snapshot = {
            "state": self._adaptation["state"],
            "planning_efficiency_pct": round(settings.round_trip_efficiency * 100, 2),
            "effective_minimum_profit_dkk_per_kwh": settings.minimum_profit_dkk_per_kwh,
            "forecast_source": self._forecast_planning_source,
            "forecast_uncertainties": {
                source: round(details["effective_uncertainty_dkk_per_kwh"], 2)
                for source, details in self._forecast_adaptation.get("sources", {}).items()
            },
        }
        previous = self.runtime.adaptive_history[-1] if self.runtime.adaptive_history else {}
        if all(previous.get(key) == value for key, value in snapshot.items()):
            return
        self.runtime.adaptive_history.append({
            "timestamp": datetime.now(UTC).isoformat(), **snapshot,
        })
        self.runtime.adaptive_history = self.runtime.adaptive_history[-100:]
        self._adaptation_dirty = True

    def _efficiency_source(self) -> str:
        """Identify the meter, load, battery and capacity used by the evidence."""
        config = self.config
        if not config.get(CONF_EFFICIENCY_GRID_POWER):
            return ""
        load = DIRECT_LOAD_SOURCE if self.is_direct_local else config.get(CONF_LOAD_POWER)
        battery = config.get(CONF_HOST) if self.is_direct_local else config.get(CONF_SOC)
        fingerprint = repr((
            config[CONF_EFFICIENCY_GRID_POWER], load, battery,
            config.get("port", DEFAULT_PORT),
            config.get("battery_charge_power_entity"),
            config.get("battery_discharge_power_entity"),
            config.get(CONF_GRID_ISOLATION_SWITCH),
            config.get(CONF_GRID_IMPORT_POWER),
            config.get(CONF_ACCOUNTING_LOAD_POWER),
            self.runtime.settings["capacity_kwh"],
        ))
        return f"{config[CONF_EFFICIENCY_GRID_POWER]}:{sha256(fingerprint.encode()).hexdigest()}"

    def _efficiency_summary(self, now: datetime) -> dict[str, Any]:
        """Configure and read qualified measurements, keeping telemetry diagnostic."""
        model = self.runtime.measured_efficiency
        source = self._efficiency_source()
        model.configure(source)
        if source:
            return model.summary(now)
        summary = self.runtime.actual_history.round_trip_summary(dt_util.as_local(now))
        summary.update({
            "method": "battery_telemetry_diagnostic",
            "source": None,
            "sample_count": 0,
            "planning_efficiency_pct": None,
            "accepted_windows": [],
            "reason": "Bind a true active-power meter to enable matched-SOC efficiency learning",
        })
        return summary

    def _planning_efficiency(self, now: datetime | None = None) -> tuple[float, str]:
        """Prefer qualified matched-SOC evidence, otherwise retain the fallback."""
        if self.config.get(CONF_EFFICIENCY_GRID_POWER):
            summary = self._efficiency_summary(now or datetime.now(UTC))
            measured = summary.get("planning_efficiency_pct")
            if isinstance(measured, (int, float)) and math.isfinite(measured) and 50 <= measured <= 100:
                return measured / 100, "matched_soc_grid_meter"
            # A newly bound meter must not silently use incompatible legacy
            # learning while its independent evidence is still qualifying.
        elif learned := self.runtime.battery_learner.learned_efficiency:
            return learned, "legacy_balanced_flow"
        return self.runtime.settings["round_trip_efficiency"] / 100, "configured_fallback"

    def _efficiency_grid_power(self, now: datetime) -> float | None:
        """Read fresh active watts independently of the export-safety binding."""
        return self._efficiency_value(CONF_EFFICIENCY_GRID_POWER, now, "W")

    def _efficiency_value(
        self, key: str, now: datetime, unit: str | None = None
    ) -> float | None:
        """Efficiency evidence requires reports no older than ninety seconds."""
        state = self._state(key)
        if state is None or (
            unit is not None and state.attributes.get("unit_of_measurement") != unit
        ):
            return None
        reported = getattr(state, "last_reported", None) or state.last_updated
        age = (now - reported.astimezone(UTC)).total_seconds()
        if not 0 <= age <= 90:
            return None
        return fresh_numeric_value(state, now)

    def _efficiency_activity(
        self, charge_w: float | None, discharge_w: float | None
    ) -> str:
        """Classify activity only when the connected load's supply is established."""
        if any(
            value is None or not math.isfinite(value) or value < 0
            for value in (charge_w, discharge_w)
        ):
            return "unknown"
        charging, discharging = charge_w > 10, discharge_w > 10
        if charging and discharging:
            return "unknown"
        if discharging:
            # A raw inverter-output value does not prove it supplies the whole
            # load. Accept delivered load only with physical grid isolation,
            # or an independent balanced flow corroborating full supply.
            relay = self._state(CONF_GRID_ISOLATION_SWITCH)
            if relay is not None:
                return "discharging" if relay.state == "off" else "unknown"
            flow = self.power_flow
            if flow is None or not flow.flow_available:
                return "unknown"
            now = datetime.now(UTC)
            if self._efficiency_value(CONF_GRID_IMPORT_POWER, now, "W") is None:
                return "unknown"
            if self.is_direct_local and self._efficiency_value(
                CONF_ACCOUNTING_LOAD_POWER, now, "W"
            ) is None:
                return "unknown"
            load = self._load_power()
            if load is None or flow.battery_output_power_w < load - FLOW_NOISE_FLOOR_W:
                return "unknown"
            return "discharging"
        relay = self._state(CONF_GRID_ISOLATION_SWITCH)
        if self.config.get(CONF_GRID_ISOLATION_SWITCH) and (
            relay is None or relay.state != "on"
        ):
            return "unknown"
        return "charging" if charging else "idle"

    def _efficiency_load(self, now: datetime) -> float | None:
        """Require recent legacy load telemetry as well as the dedicated meter."""
        if not self.is_direct_local:
            return self._efficiency_value(CONF_LOAD_POWER, now, "W")
        return self._load_power()

    def _efficiency_healthy(self, now: datetime) -> bool:
        """Do not learn through safe stops or independently measured exports."""
        if self.runtime.pending_safe or self.runtime.export_safety_fault_latched:
            return False
        grid = self._float(CONF_GRID_IMPORT_POWER)
        return grid is None or grid >= -FLOW_NOISE_FLOOR_W

    def _partial_charge_targets(self) -> tuple[int, ...] | None:
        """Only plan ceilings supported by the bound native number control."""
        if self.is_direct_local:
            return None  # The local protocol supports integer SOC ceilings.
        control = self._state(CONF_MAX_SOC_CONTROL)
        if control is None:
            return ()
        try:
            minimum = float(control.attributes["min"])
            maximum = float(control.attributes["max"])
            step = float(control.attributes["step"])
        except (KeyError, TypeError, ValueError):
            return ()
        if not all(math.isfinite(v) for v in (minimum, maximum, step)) or step <= 0:
            return ()
        return tuple(
            target for target in range(1, 101)
            if minimum <= target <= maximum
            and abs((target - minimum) / step - round((target - minimum) / step)) < 1e-6
        )

    def _optimize_with_storage_policy(
        self,
        slots: list[PriceSlot],
        *,
        now: datetime,
        soc: float,
        settings: PlannerSettings,
        current_action: Action,
        committed_slot: PlannedSlot | None = None,
        opportunistic_enabled: bool | None = None,
        current_discharge_enabled: bool = True,
        learned_tail_start: datetime | None = None,
    ) -> OpportunisticPlanDecision:
        """Compare the normal plan with an opt-in higher-target candidate."""
        if learned_tail_start is not None:
            arguments = {
                "now": now, "soc": soc, "settings": settings,
                "current_action": current_action, "committed_slot": committed_slot,
                "opportunistic_enabled": opportunistic_enabled,
                "current_discharge_enabled": current_discharge_enabled,
            }
            candidate = self._optimize_with_storage_policy(slots, **arguments)
            if has_forecast_backed_movement(candidate.plan, learned_tail_start, settings.energy_step_wh):
                return replace(
                    candidate, learned_control_status="active",
                    learned_control_reason="A risk-adjusted battery opportunity uses the learned price intervals",
                )
            trusted = [slot for slot in slots if slot.end <= learned_tail_start]
            baseline = self._optimize_with_storage_policy(trusted, **arguments)
            return replace(
                baseline, learned_control_status="shadow",
                learned_control_reason="No battery movement justifies the learned tail; preserving the trusted-price plan",
            )
        enabled = (
            self.runtime.opportunistic_charging_enabled
            if opportunistic_enabled is None else opportunistic_enabled
        )
        # Compare both ceilings using the same continuation value. Historical
        # prices temper the forecast reference; neither changes cash savings.
        # Only credit intervals accepted by the optimizer's horizon validation.
        storage_value = relative_storage_value(
            future_price_slots(slots, now),
            historical_slots=self.runtime.sizing_history.recent_price_slots(now),
        ) if enabled else 0.0
        normal_plan = optimize(
            slots,
            now=now,
            soc=soc,
            settings=settings,
            current_action=current_action,
            committed_slot=committed_slot,
            terminal_price_floor_dkk_per_kwh=storage_value,
            current_discharge_enabled=current_discharge_enabled,
        )
        opportunistic_settings = replace(
            settings,
            target_soc=OPPORTUNISTIC_TARGET_SOC,
        )
        if (
            not enabled
            or opportunistic_settings.target_soc <= settings.target_soc
        ):
            return select_opportunistic_plan(
                normal_plan,
                normal_plan,
                settings,
                opportunistic_settings,
                enabled=enabled,
            )
        opportunistic_plan = optimize(
            slots,
            now=now,
            soc=soc,
            settings=opportunistic_settings,
            current_action=current_action,
            committed_slot=committed_slot,
            terminal_price_floor_dkk_per_kwh=storage_value,
            current_discharge_enabled=current_discharge_enabled,
        )
        decision = select_opportunistic_plan(
            normal_plan,
            opportunistic_plan,
            settings,
            opportunistic_settings,
            enabled=True,
        )
        if storage_value > 0:
            decision = replace(
                decision,
                reason=(
                    f"{decision.reason}; relative storage reference "
                    f"{storage_value:.2f} DKK/kWh from upcoming prices and "
                    "qualified recent history when available"
                ),
            )
        return decision

    def _observed_action(self) -> Action:
        if self.is_direct_local:
            return action_from_operating_mode(
                self._observed_local_mode or self._commanded_local_mode
            )
        if self.config.get(CONF_DEVICE_TYPE) == DEVICE_ECOFLOW:
            try:
                return Action(self.runtime.last_action)
            except ValueError:
                return Action.SAFE
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
        """Settle actual energy only against a validated, dated known tariff."""
        for slot in slots:
            if (
                slot.source == "known"
                and slot.start.tzinfo is not None and slot.end.tzinfo is not None
                and slot.start <= now < slot.end
                and not isinstance(slot.price, bool)
                and math.isfinite(slot.price) and -20 <= slot.price <= 100
            ):
                return slot.price
        return None

    async def _force_safe_if_needed(self, now: datetime) -> str:
        if self._unloading:
            return "integration unloading; economic commands revoked"
        if not self.runtime.execution_enabled and not self.runtime.pending_safe:
            return "automatic writes disabled"
        self.runtime.pending_safe = True
        return await self._async_retry_safe(now)

    async def _async_retry_safe(
        self, now: datetime, *, allow_unloading: bool = False
    ) -> str:
        """Complete a safety request independently of economic authorization."""
        if self._unloading and not allow_unloading:
            return "safety request superseded by integration unload"
        request_id = self._safe_request_id
        await self.store.save(self.runtime)
        if self._unloading and not allow_unloading:
            return "safety request superseded by integration unload"
        success, result = await self.actuator.async_command(
            Action.SAFE, now, grid_available=self._grid_available()
        )
        if request_id == self._safe_request_id:
            self.runtime.pending_safe = not success
        await self.store.save(self.runtime)
        self._record_command_result(success, result)
        if success and self.runtime.execution_enabled:
            return f"{result}; writes resume automatically when health recovers"
        return result

    async def _restore_grid_during_pause(self, result: str) -> str:
        """Restore mains independently of unavailable/paused inverter controls."""
        if (
            self._unloading
            or not self.is_direct_local
            or not self.runtime.execution_enabled
        ):
            return result
        if (
            self._local_snapshot is not None
            and (
                self._observed_action() is Action.BATTERY
                or self._local_snapshot.raw_reported_output_power_w > 0
            )
        ):
            # Startup/price faults must also stop old active discharge once
            # fresh telemetry exists. A TCP outage still only restores mains.
            success, detail = await self.actuator.async_command(Action.GRID, datetime.now(UTC))
            self._record_command_result(success, detail)
            return f"{result}; {detail}"
        if not self.config.get(CONF_GRID_ISOLATION_SWITCH):
            return result
        restored = await self.actuator.async_set_grid_isolation(True)
        detail = (
            "grid input confirmed on"
            if restored
            else "grid input could not be restored; will retry"
        )
        if not restored:
            self._record_command_result(False, detail)
        return f"{result}; {detail}"

    def _record_command_result(self, success: bool, result: str) -> None:
        """Count failed writes and schedule bounded integration recovery."""
        if success:
            self._consecutive_command_failures = 0
            return
        self._consecutive_command_failures += 1
        _LOGGER.warning(
            "Battery command failed (%d consecutive attempts): %s",
            self._consecutive_command_failures,
            result,
        )
        if self._consecutive_command_failures >= RECOVERY_RELOAD_AFTER_FAILURES:
            self._schedule_recovery_reload("repeated battery command failures")

    def _schedule_recovery_reload(self, reason: str) -> None:
        """Reload this config entry after repeated failures, with a cooldown."""
        if self._recovery_reload_scheduled or self.hass.is_stopping:
            return
        cooldowns = self.hass.data.setdefault(f"{DOMAIN}_recovery", {})
        now = datetime.now(UTC)
        previous = cooldowns.get(self.entry.entry_id)
        if isinstance(previous, datetime) and now - previous < RECOVERY_RELOAD_COOLDOWN:
            return
        cooldowns[self.entry.entry_id] = now
        self._recovery_reload_scheduled = True
        self._recovery_reload_reason = reason
        _LOGGER.warning(
            "Scheduling House Battery integration reload: %s", reason
        )

    async def _async_reload_for_recovery(self, reason: str) -> None:
        """Run a recovery reload after the current coordinator refresh returns."""
        await asyncio.sleep(1)
        try:
            _LOGGER.warning(
                "Reloading House Battery integration for recovery: %s", reason
            )
            reloaded = await self.hass.config_entries.async_reload(self.entry.entry_id)
            if not reloaded:
                self._recovery_reload_scheduled = False
                _LOGGER.warning("Home Assistant did not reload the House Battery entry")
        except Exception:
            self._recovery_reload_scheduled = False
            _LOGGER.exception("House Battery recovery reload failed")

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._refresh_lock:
            if self._unloading:
                return self.data or {}
            return await self._async_update_data_locked()

    async def async_prepare_unload(self) -> None:
        """Revoke old work and drain it before a replacement entry can write."""
        stop_required = self.runtime.execution_enabled or self.runtime.pending_safe
        self._unloading = True
        self._cancel_price_timer()
        self._sizing_request_key = None
        sizing_task = self._sizing_task
        if sizing_task is not None:
            sizing_task.cancel()
            try:
                await sizing_task
            except asyncio.CancelledError:
                pass
        self._planning_revision += 1
        self.actuator.invalidate_pending_commands()
        async with self._refresh_lock:
            pass
        await self.actuator.async_wait_for_idle()
        if stop_required:
            self._safe_request_id += 1
            self.runtime.pending_safe = True
            await self._async_retry_safe(datetime.now(UTC), allow_unloading=True)

    async def async_cancel_unload(self) -> None:
        """Resume fresh validation when Home Assistant cannot unload platforms."""
        self._planning_revision += 1
        self._unloading = False
        self._startup_guard_passed = False
        self._recovery_reload_scheduled = False
        if self.runtime.execution_enabled:
            self._startup_control_gate_reason = (
                "Awaiting fresh validation after an unsuccessful integration unload"
            )
        await self.async_request_refresh()

    async def _async_update_data_locked(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        self._economic_plan_reused = False
        self.runtime.prune(now)
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
                self._consecutive_local_failures = 0
            except LocalProtocolError as exc:
                self._local_snapshot = None
                self._local_controls = {}
                self._observed_local_mode = None
                local_problem = f"battery local TCP unavailable: {exc}"
                if self._local_tcp_unavailable_since is None:
                    self._local_tcp_unavailable_since = now
                self._consecutive_local_failures += 1
                elapsed = now - self._local_tcp_unavailable_since
                if elapsed < LOCAL_TCP_RECOVERY_GRACE:
                    local_tcp_recovery_remaining = LOCAL_TCP_RECOVERY_GRACE - elapsed
                elif self._consecutive_local_failures >= RECOVERY_RELOAD_AFTER_FAILURES:
                    self._schedule_recovery_reload(
                        "repeated local TCP telemetry/control failures"
                    )
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
        self.power_flow = self._power_flow()
        slots = self._price_slots(now)
        self._last_price_slots = slots
        self._schedule_price_refresh(slots, now)
        action = self._observed_action()
        grid_available = self._grid_available()
        price = self._current_price(slots, now)
        local_power = (
            observed_battery_power(self._local_snapshot, self._local_controls)
            if self.is_direct_local and self._local_snapshot is not None
            else None
        )
        actual_charge_power = (
            local_power[0]
            if local_power is not None
            else self.power_flow.battery_charge_power_w
            if self.power_flow is not None
            else None
        )
        actual_discharge_power = (
            local_power[1]
            if local_power is not None
            else self.power_flow.battery_output_power_w
            if self.power_flow is not None
            else None
        )
        local_now = dt_util.as_local(now)
        sizing_source = self._sizing_source()
        old_sizing_source = self.runtime.sizing_history.source
        self.runtime.sizing_history.configure(sizing_source)
        if old_sizing_source != sizing_source:
            self._sizing_request_key = None
        current_price_slot = next((
            slot for slot in slots if slot.start <= now < slot.end and slot.source == "known"
        ), None)
        planning_evidence_scope = evidence_scope(
            self.config, direct=self.is_direct_local, time_zone=self.hass.config.time_zone,
        )
        planning_evidence_changed, planning_evidence_error = capture_evidence(
            lambda: observe_actual_evidence(
                self.runtime, now, scope=planning_evidence_scope,
                load_w=self._load_power() if not problems and not local_problem else None,
                price=current_price_slot.price if current_price_slot else None,
            )
        )
        sizing_should_save = self.runtime.sizing_history.observe(
            now,
            load_w=self._load_power()
            if not local_problem and grid_available is True else None,
            price=current_price_slot.price if current_price_slot else None,
            price_start=(current_price_slot.tariff_start or current_price_slot.start)
            if current_price_slot else None,
        ) or old_sizing_source != sizing_source
        actual_history_should_save = self.runtime.actual_history.record(
            local_now,
            soc,
            actual_charge_power,
            actual_discharge_power,
            price,
        )
        # Efficiency learning has its own real-power source and quality gates.
        # Missing optional measurement inputs invalidate its window without
        # becoming a control/safety failure for an otherwise healthy battery.
        # TCP reads await I/O: a meter report arriving meanwhile is newer than
        # the planning timestamp, but is the freshest valid observation.
        measurement_now = datetime.now(UTC)
        self.runtime.measured_efficiency.configure(self._efficiency_source())
        efficiency_should_save = False
        if self.config.get(CONF_EFFICIENCY_GRID_POWER):
            efficiency_should_save = self.runtime.measured_efficiency.observe(
                measurement_now,
                soc=soc if self.is_direct_local else self._efficiency_value(CONF_SOC, measurement_now),
                grid_power_w=self._efficiency_grid_power(measurement_now),
                load_power_w=self._efficiency_load(measurement_now),
                activity=self._efficiency_activity(
                    actual_charge_power, actual_discharge_power
                ),
                reported_charge_power_w=actual_charge_power,
                healthy=(
                    not problems and not local_problem and not local_tcp_recovering
                    and grid_available is True
                    and self._efficiency_healthy(measurement_now)
                ),
                capacity_kwh=self.runtime.settings["capacity_kwh"],
                delivery_topology=(
                    "mains_isolated" if self.is_direct_local
                    and self.config.get(CONF_GRID_ISOLATION_SWITCH) else "grid_parallel"
                ),
            )
        round_trip_summary = self._efficiency_summary(measurement_now)
        planning_efficiency, planning_efficiency_source = self._planning_efficiency(measurement_now)
        round_trip_summary.update({
            "configured_efficiency_pct": self.runtime.settings["round_trip_efficiency"],
            "effective_planning_efficiency_pct": planning_efficiency * 100,
            "planning_efficiency_source": planning_efficiency_source,
        })
        await self.evidence.async_observe(
            now,
            price,
            action,
            soc,
            battery_charge_power_w=actual_charge_power,
            battery_discharge_power_w=actual_discharge_power,
            battery_learning_enabled=not self.is_direct_local,
        )

        # The grid meter is the authoritative zero-export safety signal. Export
        # is only ever observed, never commanded; a large or persistent export
        # while automatic control is enabled fails the optimizer safe and
        # disables writes. The hysteresis between ``FLOW_NOISE_FLOOR_W`` and
        # ``EXPORT_SAFETY_W`` keeps a few-watt meter fluctuation from latching.
        direct_grid_power = (
            self._float(CONF_GRID_IMPORT_POWER)
            if self.is_direct_local and self.config.get(CONF_GRID_IMPORT_POWER)
            else None
        )
        export_w = (
            self.power_flow.grid_export_w
            if self.power_flow
            else normalize_grid_flow(direct_grid_power)[1]
            if direct_grid_power is not None
            else None
        )
        export_detected = (
            export_w > FLOW_NOISE_FLOOR_W if export_w is not None else None
        )
        if (
            self.runtime.execution_enabled
            and export_w is not None
            and export_w > EXPORT_SAFETY_W
            and not self.runtime.export_safety_fault_latched
        ):
            self.runtime.export_safety_fault_latched = True
            await self.store.save(self.runtime)
        export_safety_fault = (
            self.runtime.export_safety_fault_latched if export_w is not None else None
        )
        if self.runtime.execution_enabled and self.runtime.export_safety_fault_latched:
            problems.append(
                "grid export safety fault is latched; turn automatic control off "
                "then on after confirming export has stopped"
            )

        state = "BOOTSTRAP"
        reason = "Waiting for valid local telemetry and price intervals"
        command_result = "no command"
        effective_settings: PlannerSettings | None = None
        executable_slot: dict[str, Any] | None = None
        executable_action: Action | None = None
        opportunistic_active = False
        opportunistic_reason = "No valid plan is available"
        opportunistic_incremental_savings = 0.0
        startup_waiting = (
            self._startup_control_gate_reason is not None
            and not local_tcp_recovering
            and direct_load_problem is None
            and (bool(problems) or grid_available is not True)
        )
        if problems or grid_available is not True or local_tcp_recovering or self.runtime.pending_safe:
            self._economic_plan_key = None
            self._economic_decision = None
        if self.runtime.pending_safe:
            self.plan = None
            state = "RECOVERING"
            reason = "Completing the pending safe stop before economic control resumes"
            command_result = await self._async_retry_safe(now)
        elif startup_waiting:
            self.plan = None
            state = "BOOTSTRAP"
            reason = (
                "Awaiting fresh post-restart telemetry and control validation: "
                + "; ".join(problems or ["grid availability is not confirmed"])
            )
            command_result = await self._restore_grid_during_pause(
                "startup gate is inhibiting economic writes"
            )
        elif grid_available is False:
            self.plan = None
            state = "OUTAGE"
            reason = (
                "Grid is unavailable; requesting the absolute emergency SOC floor; "
                "physical backup-output cutoff is not established by read-back"
            )
            command_result = await self._force_safe_if_needed(now)
        elif problems:
            state = "DEGRADED"
            reason = "; ".join(problems)
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
            # A brief missing read is not a mode change. In particular, leave
            # the mains plug as it is so reconnecting can resume the same slot.
            command_result = "automatic writes paused during local TCP recovery grace period"
        elif soc is not None:
            try:
                settings = self._settings()
                settings = apply_storage_policy(settings)
                current_action = action if action is not Action.SAFE else Action.GRID
                native_ceiling = (
                    _control_number(self._local_controls, "3024")
                    if self.is_direct_local else self._float(CONF_MAX_SOC_CONTROL)
                )
                if (
                    current_action is Action.CHARGE and native_ceiling is not None
                    and soc >= native_ceiling and actual_charge_power is not None
                    and actual_charge_power <= FLOW_UI_ACTIVE_THRESHOLD_W
                ):
                    # A latched Charge command can already be physically idle
                    # at its ceiling. Starting a new charge is then a restart;
                    # the published current-tariff commitment still applies.
                    current_action = Action.GRID
                committed_slot = (
                    self.runtime.daily_plan.slot_at(now)
                    if self._override_action() is None else None
                )
                if committed_slot is not None and committed_slot.action is Action.CHARGE:
                    # Plan from the same mode and ceiling that execution holds
                    # until the tariff boundary, including after a disconnect.
                    allowed_target = (
                        OPPORTUNISTIC_TARGET_SOC
                        if self.runtime.opportunistic_charging_enabled
                        else settings.target_soc
                    )
                    committed_slot = replace(
                        committed_slot,
                        charge_target_soc=min(
                            allowed_target,
                            committed_slot.charge_target_soc
                            if committed_slot.charge_target_soc is not None
                            else math.ceil(committed_slot.soc_end),
                        ),
                    )
                revision = self._planning_revision
                current_discharge_allowed = settings.control_capabilities.discharge_load_feasible(
                    settings.discharge_power_w, self._load_power()
                )
                # Numerical refinements can wait for the next price event;
                # qualification withdrawal cannot retain an expired policy or
                # a learned forecast that no longer passes its admission gate.
                model_eligibility = (
                    self._adaptation.get("state"), planning_efficiency_source,
                    self._forecast_adaptation.get("priority_adaptive_ready", False),
                    tuple((source, details.get("adaptive_ready", False)) for source, details
                          in sorted(self._forecast_adaptation.get("sources", {}).items())),
                    tuple((source, tuple((band, details.get("reason") == "validated_bias_correction")
                                         for band, details in sorted(
                                             source_data.get("calibration", {}).get("lead_bands", {}).items())))
                          for source, source_data in sorted(self._forecast_sources.items())
                          if "calibration" in source_data),
                    tuple(sorted(self._forecast_sources.get("learned_pattern", {}).get(
                        "evidence", {}).get("selected_models", {}).items())),
                    # A profile hour may lose its required independent dates
                    # while the selected model remains qualified. Coverage is
                    # an admission boundary; numeric refinements are not.
                    self._pattern_supported_intervals,
                )
                economic_key = planning_key(
                    slots, revision=revision, settings=settings,
                    forecast_enabled=self.runtime.forecast_enabled,
                    opportunistic_enabled=self.runtime.opportunistic_charging_enabled,
                    scheduled_loads=self.runtime.scheduled_loads, soc=soc,
                    current_discharge_enabled=current_discharge_allowed,
                    override_action=self.runtime.override_action,
                    market_inputs=self._economic_market_inputs,
                    configured_settings=self.runtime.settings,
                    model_eligibility=model_eligibility,
                )
                if (
                    economic_key == self._economic_plan_key
                    and self._economic_decision is not None
                    and self.plan is self._economic_decision.plan
                    and self.plan.slots
                    and self.plan.slots[0].start <= now < self.plan.slots[0].end
                ):
                    storage_decision = self._economic_decision
                    self._economic_plan_reused = True
                else:
                    plan_trigger = planning_change_reason(self._economic_plan_key, economic_key)
                    storage_decision = await self.hass.async_add_executor_job(
                        partial(
                            self._optimize_with_storage_policy,
                            list(slots),
                            now=now,
                            soc=soc,
                            settings=settings,
                            current_action=current_action,
                            committed_slot=committed_slot,
                            current_discharge_enabled=current_discharge_allowed,
                            opportunistic_enabled=(
                                self.runtime.opportunistic_charging_enabled
                            ),
                            **({"learned_tail_start": self._pattern_tail_start}
                               if self._pattern_tail_start is not None else {}),
                        )
                    )
                if self._unloading:
                    return self.data or {}
                if revision != self._planning_revision:
                    raise ValueError("Planning inputs changed; awaiting a fresh plan")
                latest_grid = self._grid_available()
                latest_problems = get_health_problems(
                    self.hass, self.config, datetime.now(UTC)
                )
                if latest_grid is not True or latest_problems:
                    grid_available = latest_grid
                    problems.extend(p for p in latest_problems if p not in problems)
                    raise ValueError("Physical inputs changed while planning")
                self._economic_plan_key = economic_key
                self._economic_decision = storage_decision
                if not self._economic_plan_reused:
                    self._economic_plan_last_trigger = plan_trigger
                # A meter can remain healthy while changing from import to
                # export during the worker calculation. Reapply that safety
                # signal immediately before publishing/commanding its result.
                if self.config.get(CONF_GRID_IMPORT_POWER):
                    latest_meter = self._float(CONF_GRID_IMPORT_POWER)
                    if latest_meter is not None:
                        export_w = normalize_grid_flow(latest_meter)[1]
                        export_detected = export_w > FLOW_NOISE_FLOOR_W
                        direct_grid_power = latest_meter if self.is_direct_local else None
                        if (
                            self.runtime.execution_enabled
                            and export_w > EXPORT_SAFETY_W
                        ):
                            self.runtime.export_safety_fault_latched = True
                            export_safety_fault = True
                            await self.store.save(self.runtime)
                            raise ValueError("Grid export detected while planning; control paused")
                latest_soc = self._float(CONF_SOC)
                if (
                    latest_soc is not None
                    and latest_soc <= settings.reserve_soc < soc
                ):
                    # Bound HA sensors can report the floor while the worker
                    # runs. Rebuild just this interval from that observation
                    # so the held discharge is cut now; the next refresh plans
                    # the full horizon. A single interval has only three
                    # transitions and does not need a long worker calculation.
                    soc = latest_soc
                    storage_decision = self._optimize_with_storage_policy(
                        slots[:1], now=now, soc=soc, settings=settings,
                        current_action=Action.GRID, opportunistic_enabled=False,
                    )
                self.plan = storage_decision.plan
                self._pattern_control_status = storage_decision.learned_control_status
                self._pattern_control_reason = storage_decision.learned_control_reason
                if storage_decision.learned_control_status == "shadow":
                    self._forecast_used_slot_count -= self._pattern_used_slots
                    self._forecast_sources["learned_pattern"]["status"] = "shadow"
                    self._forecast_sources["learned_pattern"]["used_slots"] = 0
                    if self._forecast_planning_source == "learned_pattern":
                        self._forecast_planning_source = None
                        self._forecast_status = "shadow"
                effective_settings = storage_decision.settings
                if self._override_action() is not None:
                    effective_settings = replace(
                        effective_settings, target_soc=settings.target_soc
                    )
                opportunistic_active = storage_decision.active
                if self._override_action() is not None:
                    opportunistic_active = False
                opportunistic_reason = storage_decision.reason
                opportunistic_incremental_savings = (
                    storage_decision.incremental_savings_dkk
                )
                if not self._economic_plan_reused:
                    captured, capture_error = capture_evidence(
                        lambda: record_plan_evidence(
                            self.runtime, now, slots=slots, settings=effective_settings,
                            soc=soc, current_action=current_action, scope=planning_evidence_scope,
                            committed_slot=committed_slot,
                            terminal_price_floor_dkk_per_kwh=self.plan.terminal_price_floor_dkk_per_kwh,
                            current_discharge_enabled=current_discharge_allowed,
                            forecast_load=lambda start, end: self._forecast_load_wh(
                                start, end, now, self._load_power()
                            ),
                        ),
                    )
                    planning_evidence_changed = captured or planning_evidence_changed
                    planning_evidence_error = capture_error or planning_evidence_error
                if self._override_action() is not None:
                    opportunistic_reason = (
                        f"Manual override uses configured target {settings.target_soc:g}%"
                    )
                    opportunistic_incremental_savings = 0.0
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
                    command_result = await self._restore_grid_during_pause(
                        "startup gate is inhibiting economic writes"
                    )
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
                reserve_reached = soc <= effective_settings.reserve_soc
                previous_slot = self.runtime.daily_plan.slot_at(now)
                reserve_plan_cut = (
                    reserve_reached
                    and previous_slot is not None
                    and previous_slot.action is Action.BATTERY
                )
                if not self._economic_plan_reused or reserve_plan_cut:
                    self.runtime.daily_plan = reconcile_daily_plan(
                        self.runtime.daily_plan,
                        self.plan.slots,
                        cutoff=now,
                        day_start=day_start,
                        horizon_end=horizon_end,
                        # Physical SOC and load eligibility still interrupt a
                        # held discharge between economic planning events.
                        discharge_enabled=(
                            effective_settings.discharge_enabled and not reserve_reached
                            and effective_settings.control_capabilities.discharge_load_feasible(
                                effective_settings.discharge_power_w, self._load_power()
                            )
                        ),
                        charge_enabled=effective_settings.charge_enabled,
                    )
                    self._daily_plan_dirty = True
                published_slot = self.runtime.daily_plan.slot_at(now)
                if published_slot is not None:
                    if (
                        published_slot.action is Action.CHARGE
                        and self._override_action() is None
                    ):
                        # Execution owns this interval's ceiling in both
                        # directions. A future 100% opportunity must not raise
                        # a held 90% slot, or lower a held full charge early.
                        # User opt-out/lower limits remain effective immediately;
                        # manual overrides continue to use their normal target.
                        allowed_target = (
                            OPPORTUNISTIC_TARGET_SOC
                            if self.runtime.opportunistic_charging_enabled
                            else settings.target_soc
                        )
                        published_target = (
                            published_slot.charge_target_soc
                            if published_slot.charge_target_soc is not None
                            else math.ceil(published_slot.soc_end)
                        )
                        held_target = min(allowed_target, published_target)
                        if held_target != effective_settings.target_soc:
                            effective_settings = replace(
                                effective_settings, target_soc=held_target
                            )
                            opportunistic_active = held_target > settings.target_soc
                            opportunistic_reason = (
                                f"Published charge target {held_target:g}% remains in "
                                f"force until {published_slot.end.isoformat()}"
                            )
                    executable_slot = published_slot.as_dict()
                    executable_action = published_slot.action
                commissioned = bool(self.config.get(CONF_COMMISSIONED, False))
                state = (
                    "ACTIVE"
                    if commissioned and self.runtime.execution_enabled
                    else "SHADOW"
                )
                reason = self.plan.reason
                if (
                    published_slot is not None
                    and published_slot.action is not self.plan.current_action
                ):
                    reason = (
                        f"Published {published_slot.action.value} action remains in "
                        f"force until {published_slot.end.isoformat()}; "
                        f"latest optimizer recommendation: {self.plan.reason}"
                    )
                if not self._startup_guard_passed:
                    self._startup_guard_passed = True
                    command_result = await self._restore_grid_during_pause(
                        "startup freshness gate passed; no economic write on first refresh"
                    )
                elif state == "ACTIVE":
                    # A manual override forces the commanded battery action
                    # regardless of the optimizer plan, so each physical function
                    # can be verified in isolation. `auto` follows the plan.
                    override_action = self._override_action()
                    # The daily-plan reconciler holds the published action
                    # through the current tariff boundary. Follow that same
                    # slot so the timeline, execution diagnostic, and command
                    # do not disagree during an economic replan.
                    requested_action = (
                        override_action
                        if override_action is not None
                        else executable_action or self.plan.current_action
                    )
                    reserve_exit = (
                        reserve_reached
                        and (
                            requested_action is Action.BATTERY
                            or (reserve_plan_cut and requested_action is Action.GRID)
                        )
                    )
                    if reserve_exit:
                        # Native discharge limits do not reconnect an isolated
                        # grid input. Actual SOC must override a held tariff
                        # slot and a manual battery override immediately.
                        requested_action = Action.GRID
                        reason = (
                            f"Actual SOC {soc:g}% is at or below reserve "
                            f"{effective_settings.reserve_soc:g}%; restoring grid"
                        )
                    if requested_action is Action.CHARGE and not effective_settings.charge_enabled:
                        requested_action = Action.GRID
                        reason = "EcoFlow AC Charging Speed is unavailable; charging disabled"
                    # Direct-local units have no CT/grid-flow signal, so cap a
                    # manual discharge slot below the fresh connected-load
                    # reading. Missing load telemetry fails closed to 0 W.
                    if self.runtime.execution_enabled and not self.runtime.pending_safe:
                        command_ok, command_result = await self.actuator.async_command(
                            requested_action,
                            now,
                            target_soc=effective_settings.target_soc,
                            load_w=self._load_power(),
                        )
                        superseded = command_result == (
                            "command superseded by a safety request"
                        )
                        if not superseded:
                            self._record_command_result(command_ok, command_result)
                        if reserve_exit:
                            command_result = f"reserve SOC protection: {command_result}"
                    else:
                        superseded = True
                        command_result = "economic command superseded by a safety request"
                    if superseded or not self.runtime.execution_enabled:
                        state = (
                            "RECOVERING" if self.runtime.execution_enabled else "SHADOW"
                        )
                        reason = "Economic control paused by a safety request"

        decision_key = (state, reason)
        decision_changed = decision_key != self._last_decision_key
        if decision_changed:
            self.runtime.decisions.append(
                {
                    "timestamp": now.isoformat(),
                    "state": state,
                    "action": (executable_action or self.plan.current_action).value
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
        # Even paused/control-error branches publish adaptation diagnostics.
        # Compute them before the final save so their audit survives a restart.
        runtime_settings = self._settings()
        if (
            decision_changed
            or self._forecast_evidence_changed
            or self._price_patterns_changed
            or self._daily_plan_dirty
            or actual_history_should_save
            or efficiency_should_save
            or self._adaptation_dirty
            or sizing_should_save
            or planning_evidence_changed
        ):
            # RuntimeStore writes atomically, so persisting the whole runtime
            # publishes the reconciled daily plan without risk of a partial
            # timeline corrupting the stored day.  Clear the flag afterwards so a
            # no-op refresh does not rewrite storage every minute.
            await self.store.save(self.runtime)
            self._daily_plan_dirty = False
            self._adaptation_dirty = False
            self._price_patterns_changed = False

        period_bounds = calendar_period_bounds(local_now)
        periods = {
            name: self.runtime.ledger.totals_between(
                start.astimezone(UTC), end.astimezone(UTC)
            )
            for name, (start, end) in period_bounds.items()
        }
        capacity_kwh = runtime_settings.capacity_wh / 1000
        nominal_capacity_kwh = self.runtime.settings["capacity_kwh"]
        load_forecast_evidence = self.runtime.load_forecast_evidence.summary(now)
        known_source_metadata = {}
        forecast_source_metadata = {}
        for entity_id in self.config.get(CONF_PRICE_ENTITIES, []):
            source_state = self.hass.states.get(entity_id)
            if source_state is not None:
                target = (forecast_source_metadata if is_forecast_data(dict(source_state.attributes))
                          else known_source_metadata)
                target[entity_id] = dict(source_state.attributes)
        for entity_id in self._forecast_entities():
            source_state = self.hass.states.get(entity_id)
            if source_state is not None:
                forecast_source_metadata[entity_id] = dict(source_state.attributes)
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
        flow = self.power_flow
        local_snapshot = self._local_snapshot if self.is_direct_local else None
        raw_charge_power = (
            local_snapshot.raw_reported_charge_power_w
            if local_snapshot is not None
            else None
        )
        raw_output_power = (
            local_snapshot.raw_reported_output_power_w
            if local_snapshot is not None
            else None
        )
        load_power = flow.load_w if flow else self._load_power()
        battery_charge_power = (
            actual_charge_power
            if local_snapshot is not None
            else flow.battery_charge_power_w if flow else None
        )
        battery_output_power = (
            actual_discharge_power
            if local_snapshot is not None
            else flow.battery_output_power_w if flow else None
        )
        battery_power = (
            actual_discharge_power - actual_charge_power
            if local_snapshot is not None
            and actual_discharge_power is not None
            and actual_charge_power is not None
            else flow.battery_net_power_w if flow else None
        )
        grid_power = (
            flow.grid_power_w
            if flow
            else direct_grid_power
        )
        grid_import_power = (
            flow.grid_import_w
            if flow
            else normalize_grid_flow(direct_grid_power)[0]
            if direct_grid_power is not None
            else None
        )
        grid_export_power = (
            flow.grid_export_w
            if flow
            else normalize_grid_flow(direct_grid_power)[1]
            if direct_grid_power is not None
            else None
        )
        data = {
            "system_state": state,
            "healthy": (
                not problems and not local_tcp_recovering and grid_available is True
                and not self.runtime.pending_safe
            ),
            "reason": reason,
            "observed_action": action.value,
            "execution_enabled": self.runtime.execution_enabled,
            "safe_stop_pending": self.runtime.pending_safe,
            "backup_cutoff_verified": self.runtime.backup_cutoff_verified,
            "battery_discharge_blocked": self.battery_discharge_blocked,
            "battery_discharge_blocked_reason": None,
            "mode_override": self.runtime.override_action,
            "automatic_control_startup_gate": self._startup_control_gate_reason,
            "commissioned": bool(self.config.get(CONF_COMMISSIONED, False)),
            "grid_available": grid_available,
            "command_result": command_result,
            "soc": soc,
            # Direct-local battery sensors always use device telemetry. Its
            # optional independent grid meter supplies whole-house accounting.
            "load_power_w": load_power,
            "battery_runtime_hours": estimate_battery_runtime_hours(
                soc, load_power, runtime_settings
            ),
            "battery_runtime_reserve_soc": runtime_settings.reserve_soc,
            "grid_flow_power_w": grid_power,
            "grid_import_power_w": grid_import_power,
            "grid_export_power_w": grid_export_power,
            "battery_power_w": battery_power,
            "battery_charge_power_w": battery_charge_power,
            "battery_output_power_w": battery_output_power,
            "raw_reported_battery_charge_power_w": raw_charge_power,
            "raw_reported_battery_output_power_w": raw_output_power,
            "battery_power_source": (
                "device telemetry"
                if local_snapshot is not None
                else "unavailable"
                if self.is_direct_local
                else "load/grid balance"
            ),
            "direct_local": self.is_direct_local,
            "control_capabilities": runtime_settings.control_capabilities.as_dict(),
            "power_source": self._classify_power_source(flow),
            "export_detected": export_detected,
            "export_safety_fault": export_safety_fault,
            "export_power_w": export_w,
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
            "tomorrow_expected_savings_dkk": (
                self.plan.expected_savings_for_day(local_now + timedelta(days=1))
                if self.plan else None
            ),
            "realized_savings_dkk": self.plan.realized_savings_dkk
            if self.plan
            else None,
            "terminal_value_dkk": (
                self.plan.terminal_value_dkk if self.plan else None
            ),
            "expected_cost_dkk": self.plan.expected_cost_dkk if self.plan else None,
            "baseline_cost_dkk": self.plan.baseline_cost_dkk if self.plan else None,
            "planned_battery_throughput_kwh": self.plan.battery_throughput_kwh
            if self.plan
            else None,
            "terminal_price_dkk_per_kwh": self.plan.terminal_price_dkk_per_kwh
            if self.plan
            else None,
            "terminal_inventory_price_dkk_per_kwh": (
                self.plan.terminal_inventory_price_dkk_per_kwh if self.plan else None
            ),
            "effective_target_soc": effective_settings.target_soc
            if effective_settings
            else None,
            "opportunistic_charging_enabled": (
                self.runtime.opportunistic_charging_enabled
            ),
            "opportunistic_incremental_savings_dkk": round(
                opportunistic_incremental_savings, 4
            ),
            # Retain the existing storage-policy sensor contract while exposing
            # the safer opt-in implementation behind it.
            "extra_storage_active": opportunistic_active,
            "extra_storage_reason": opportunistic_reason,
            "plan_created_at": (
                self.plan.created_at.isoformat() if self.plan else None
            ),
            "plan": (self.plan.today_dict(local_now) if self.plan else None),
            # The reconciled slot is both the currently published action and the
            # one automatic control follows through the current price boundary.
            "executable_slot": executable_slot,
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
            "actual_history_date": local_now.date().isoformat(),
            "actual_history_blocks": self.runtime.actual_history.today(local_now),
            "measured_round_trip_efficiency": round_trip_summary,
            "actual_history_days_available": sorted(
                self.runtime.actual_history.days
            ),
            **periods,
            "accounting_period_starts": {
                name: start.isoformat() for name, (start, _) in period_bounds.items()
            },
            "lifetime_charge_kwh": self.runtime.ledger.total_charge_kwh,
            "lifetime_discharge_kwh": self.runtime.ledger.total_discharge_kwh,
            "lifetime_charge_cost_dkk": (
                self.runtime.ledger.total_charge_cost_dkk
                if self.runtime.ledger.charge_cost_intervals
                else None
            ),
            "lifetime_discharge_value_dkk": (
                self.runtime.ledger.total_discharge_value_dkk
                if self.runtime.ledger.discharge_value_intervals
                else None
            ),
            "lifetime_battery_net_savings_dkk": (
                self.runtime.ledger.total_battery_net_savings_dkk
            ),
            "lifetime_net_savings_dkk": (
                self.runtime.ledger.total_net_savings_dkk
                if self.runtime.ledger.savings_intervals
                else None
            ),
            "equivalent_full_cycles": equivalent_cycles,
            "estimated_degradation_pct": degradation_pct,
            "estimated_remaining_capacity_pct": 100 - degradation_pct,
            "learned_capacity_kwh": (
                self.runtime.battery_learner.learned_capacity_wh / 1000
                if self.runtime.battery_learner.learned_capacity_wh
                else None
            ),
            "learned_efficiency_pct": (
                round_trip_summary.get("planning_efficiency_pct")
                if self.config.get(CONF_EFFICIENCY_GRID_POWER)
                else self.runtime.battery_learner.learned_efficiency * 100
                if self.runtime.battery_learner.learned_efficiency else None
            ),
            "planning_efficiency_pct": planning_efficiency * 100,
            "planning_efficiency_source": planning_efficiency_source,
            "load_learning_confidence_pct": self.runtime.load_learner.confidence * 100,
            "load_forecast_error_w": load_forecast_evidence["mae_w"],
            "load_model_smoothing_error_w": self.runtime.load_learner.mean_absolute_error_w,
            "load_forecast_status": self.runtime.load_learner.baseline_status(
                now, self.runtime.scheduled_loads
            ),
            "issued_load_forecast_accuracy": load_forecast_evidence,
            "policy_replay_evidence": self.runtime.replay_history.summary(),
            "planning_evidence_error": planning_evidence_error,
            "price_input_diagnostics": {
                "known": diagnose_price_sources(known_source_metadata),
                "forecast": diagnose_price_sources(forecast_source_metadata),
                "basis": "supplied_all_in_DKK_per_kWh",
            },
            "price_patterns": self.runtime.price_patterns.summary(now) | {
                "enabled": self.runtime.forecast_enabled,
                "available_slots": self._pattern_used_slots,
                "used_slots": self._pattern_used_slots if self._pattern_control_status == "active" else 0,
                "control_status": self._pattern_control_status if self._pattern_tail_start else "not_available",
                "control_reason": self._pattern_control_reason if self._pattern_tail_start else None,
            },
            "economic_plan_reused": self._economic_plan_reused,
            "economic_plan_last_trigger": self._economic_plan_last_trigger,
            "economic_plan_created_at": self.plan.created_at.isoformat() if self.plan else None,
            "next_economic_plan_boundary": (
                self._price_timer_at.isoformat() if self._price_timer_at else None
            ),
            "learning_observations": self.runtime.load_learner.observations,
            "price_forecast_enabled": self.runtime.forecast_enabled,
            "price_forecast_source": self._forecast_source,
            "price_forecast_planning_source": self._forecast_planning_source,
            "price_forecast_sources": self._forecast_sources,
            "price_forecast_status": self._forecast_status,
            "price_forecast_last_updated": self._forecast_last_updated,
            "price_forecast_available_slots": self._forecast_slot_count,
            "price_forecast_used_slots": self._forecast_used_slot_count,
            # Extreme price indicator is separate from forecast qualification.
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
            ) if not self.config.get(CONF_EFFICIENCY_GRID_POWER) else round_trip_summary.get("sample_count", 0),
            "efficiency_learning_ready": (
                planning_efficiency_source == "matched_soc_grid_meter"
                if self.config.get(CONF_EFFICIENCY_GRID_POWER)
                else self.runtime.battery_learner.efficiency_ready
            ),
            "scheduled_load_count": len(self.runtime.scheduled_loads),
            "recent_decisions": self.runtime.decisions[-20:],
            "health_problems": reported_health_problems,
            "consecutive_local_failures": self._consecutive_local_failures,
            "consecutive_command_failures": self._consecutive_command_failures,
            "last_refresh": now.isoformat(),
        }
        data.update(plan_economics(self.plan, runtime_settings))
        data["battery_sizing"] = self._battery_sizing_status(
            local_now, runtime_settings, round_trip_summary,
        )
        data["adaptation"] = {
            **self._adaptation,
            "planning_efficiency_pct": planning_efficiency * 100,
            "planning_efficiency_source": planning_efficiency_source,
            "measurement_entity": self.config.get(CONF_EFFICIENCY_GRID_POWER),
            "planning_capacity_kwh": capacity_kwh,
            "capacity_source": "learned" if learned_capacity else "configured",
            "load_learning_confidence_pct": self.runtime.load_learner.confidence * 100,
            "forecast_adaptation": self._forecast_adaptation,
            "recent_changes": self.runtime.adaptive_history[-10:],
        }
        forecast_ready = any(
            details["adaptive_ready"]
            for details in self._forecast_adaptation.get("sources", {}).values()
        )
        forecasting_adaptive = self.runtime.adaptive_enabled and self.runtime.forecast_enabled
        data["adaptation"]["recommendations"] = [
            *self._adaptation["recommendations"],
            {
                "id": "forecast_reliability",
                "severity": "info",
                "status": "automatic" if forecast_ready else "awaiting_measurement"
                if forecasting_adaptive else "disabled",
                "message": (
                    "Forecast error measurements automatically adjust uncertainty; "
                    "provider preference changes only after comparable evidence"
                    if forecast_ready else
                    "Collecting genuine forecast comparisons: at least 24 covered hours "
                    "across three days are required for automatic tuning"
                    if forecasting_adaptive else
                    "Automatic forecast tuning is off; configured uncertainty is retained"
                ),
                "evidence": self._forecast_adaptation,
            },
        ]
        if self._recovery_reload_scheduled and self._recovery_reload_reason:
            # Dispatch only after the refresh has assembled/published its state;
            # the short delay also lets a config entry finish initial setup.
            reason = self._recovery_reload_reason
            self._recovery_reload_reason = None
            self.hass.async_create_task(self._async_reload_for_recovery(reason))
        return data

    def _battery_sizing_status(
        self, now: datetime, settings: PlannerSettings, efficiency_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish cached sizing evidence and schedule bounded offline evaluation."""
        slots, evidence = self.runtime.sizing_history.completed_slots(now)
        settings = replace(
            settings,
            target_soc=OPPORTUNISTIC_TARGET_SOC
            if self.runtime.opportunistic_charging_enabled else settings.target_soc,
        )
        values = self.runtime.settings
        options = {
            "expansion_capacity_kwh": values["sizing_expansion_capacity_kwh"],
            "expansion_cost_dkk": values["sizing_expansion_cost_dkk"],
            "payback_target_years": values["sizing_payback_target_years"],
        }
        reason = sizing_prerequisites(
            settings, evidence=evidence, efficiency_summary=efficiency_summary,
        )
        learning = {
            "state": "learning", "reason": reason or "Analyzing recorded usage and prices",
            "evidence": evidence, "current_capacity_kwh": settings.capacity_wh / 1000,
            "planning_efficiency_pct": settings.round_trip_efficiency * 100,
            "assessment_ready": reason is None,
            "efficiency_measurement_status": efficiency_summary.get("status"),
            **options,
        }
        if reason:
            self._sizing_request_key = None
            return learning
        request_key = sha256(repr((
            settings, options, self.runtime.sizing_history.source,
            efficiency_summary.get("source"),
            [(slot.start, slot.end, slot.price, slot.expected_load_wh) for slot in slots],
        )).encode()).hexdigest()
        self._sizing_request_key = request_key
        cached = self.runtime.sizing_assessment
        if cached.get("request_key") == request_key and _valid_sizing_result(cached):
            return deepcopy(cached)
        if self._sizing_retry_after is not None and now < self._sizing_retry_after:
            return {
                **learning,
                "reason": "Sizing analysis could not complete; an automatic retry is scheduled",
                "next_retry_at": self._sizing_retry_after.isoformat(),
            }
        if (self._sizing_task is None or self._sizing_task.done()) and (
            self._sizing_retry_after is None or now >= self._sizing_retry_after
        ) and not self._unloading:
            self._sizing_task = self.hass.async_create_task(
                self._async_assess_battery_size(
                    request_key, tuple(slots), settings, deepcopy(evidence),
                    deepcopy(efficiency_summary), options,
                ),
            )
        return learning

    async def _async_assess_battery_size(
        self, request_key: str, slots: tuple[PriceSlot, ...], settings: PlannerSettings,
        evidence: dict[str, Any], efficiency_summary: dict[str, Any], options: dict[str, float],
    ) -> None:
        """Keep sizing simulations outside the battery-control refresh and event loop."""
        try:
            result = await self.hass.async_add_executor_job(partial(
                assess_sizing, slots, settings, evidence=evidence,
                efficiency_summary=efficiency_summary, **options,
            ))
            if not _valid_sizing_result(result):
                raise ValueError("Sizing assessment is not a finite supported result")
            if self._unloading or self._sizing_request_key != request_key:
                return
            self.runtime.sizing_assessment = {
                **result, "request_key": request_key,
                "evaluated_at": datetime.now(UTC).isoformat(),
            }
            await self.store.save(self.runtime)
            if not self._unloading and self._sizing_request_key == request_key:
                self.async_set_updated_data({
                    **(self.data or {}),
                    "battery_sizing": deepcopy(self.runtime.sizing_assessment),
                })
            self._sizing_retry_after = None
        except Exception:
            _LOGGER.exception("Battery sizing analysis failed; normal control continues")
            self._sizing_retry_after = datetime.now(UTC) + timedelta(hours=1)
        finally:
            self._sizing_task = None

    async def async_set_execution_enabled(self, enabled: bool) -> None:
        if self._unloading:
            raise ValueError("House Battery is unloading")
        if enabled and not self.config.get(CONF_COMMISSIONED, False):
            raise ValueError(
                "Commission the integration in Options before enabling control"
            )
        if enabled and (problems := await self.async_soc_control_problems()):
            raise ValueError(
                "Automatic control requires commissioned native minimum and maximum "
                f"SOC controls: {'; '.join(problems)}"
            )
        if enabled and self.is_direct_local and (problem := self._direct_load_problem()):
            raise ValueError(
                "Automatic control requires complete battery-served off-grid "
                f"load: {problem}"
            )
        was_enabled = self.runtime.execution_enabled
        self._planning_revision += 1
        if not enabled:
            self.actuator.invalidate_pending_commands()
            self._safe_request_id += 1
            self.runtime.pending_safe = was_enabled or self.runtime.pending_safe
        if enabled and not was_enabled:
            # A prior forced selection must never become active implicitly when
            # automatic control is re-armed.
            self.runtime.override_action = OVERRIDE_AUTO
        elif not enabled:
            self.runtime.override_action = OVERRIDE_AUTO
        if enabled and not was_enabled:
            # An explicit off/on cycle is the operator reset for a confirmed
            # grid-export safety latch. If export persists, this refresh will
            # immediately latch it again before any economic write.
            self.runtime.export_safety_fault_latched = False
        self.runtime.execution_enabled = enabled
        self._startup_control_gate_reason = None
        await self.store.save(self.runtime)
        if not enabled and self.runtime.pending_safe:
            await self._async_retry_safe(datetime.now(UTC))
        await self.async_request_refresh()

    async def async_set_forecast_enabled(self, enabled: bool) -> None:
        """Opt in to external forecasts and qualified historical continuation."""
        if enabled and not self._forecast_entities() and not self.config.get(CONF_PRICE_ENTITIES):
            raise ValueError(
                "Configure at least one price entity first"
            )
        self._planning_revision += 1
        self.runtime.forecast_enabled = enabled
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_set_adaptive_enabled(self, enabled: bool) -> None:
        """Use qualified evidence for tuning without rewriting manual settings."""
        if self._unloading:
            raise ValueError("House Battery is unloading")
        self._planning_revision += 1
        self.actuator.invalidate_pending_commands()
        self.runtime.adaptive_enabled = enabled
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_set_opportunistic_charging_enabled(self, enabled: bool) -> None:
        """Opt in to a higher target only when a known profitable cycle exists."""
        self._planning_revision += 1
        self._sizing_request_key = None
        self.actuator.invalidate_pending_commands()
        self.runtime.opportunistic_charging_enabled = enabled
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_set_setting(self, key: str, value: float) -> None:
        if (
            key == "charge_power_w"
            and self.config.get(CONF_DEVICE_TYPE) == DEVICE_ECOFLOW
        ):
            try:
                entity_id = self.config.get("charge_power_control_entity")
                state = self.hass.states.get(entity_id) if entity_id else None
                if state is None:
                    return
                minimum, maximum, step = ecoflow_charge_power_range_w(state)
                if not minimum <= value <= maximum or abs(
                    (value - minimum) / step - round((value - minimum) / step)
                ) > 1e-6:
                    return
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return
        self._planning_revision += 1
        self._sizing_request_key = None
        self.actuator.invalidate_pending_commands()
        if key == "target_soc" and value < self.runtime.settings[key]:
            # An explicit user reduction cancels old held ceilings. Keep past
            # evidence intact; a newly justified opportunistic plan can still
            # use its authorized higher target on the next economic replan.
            now = datetime.now(UTC)
            self.runtime.daily_plan = replace(
                self.runtime.daily_plan,
                slots=tuple(
                    replace(
                        slot,
                        charge_target_soc=min(
                            value,
                            slot.charge_target_soc
                            if slot.charge_target_soc is not None
                            else math.ceil(slot.soc_end),
                        ),
                    )
                    if slot.action is Action.CHARGE and slot.end > now else slot
                    for slot in self.runtime.daily_plan.slots
                ),
            )
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
                maximum_soc=OPPORTUNISTIC_TARGET_SOC,
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
        requested_max = OPPORTUNISTIC_TARGET_SOC
        if not 0 <= requested_absolute_min <= requested_reserve <= requested_max <= 100:
            return ["configured SOC limits are invalid"]
        self._local_controls = controls
        return []

    async def async_set_manual_mode(self, mode: str) -> None:
        if self._unloading:
            raise ValueError("House Battery is unloading")
        if not self.local_client:
            raise ValueError("This entity is only available for direct local setup")
        controls = await self.local_client.async_read_controls()
        minimum = round(_control_number(controls, "3023") or 0)
        maximum = round(_control_number(controls, "3024") or 100)
        if mode != MODE_BATTERY:
            # Restore the grid path before leaving Battery mode.
            await self.actuator.async_set_grid_isolation(True)
        if mode == MODE_BATTERY:
            await self.local_client.async_set_self_consumption()
            if not await self.actuator.async_set_grid_isolation(False):
                # The device mode is established before opening the grid
                # connection. If the plug fails, return to Grid/Idle now.
                await self.local_client.async_set_grid_idle(minimum, maximum)
                self._local_controls = controls
                self._set_commanded_local_mode("Idle")
                await self.actuator.async_set_grid_isolation(True)
                await self.async_request_refresh()
                return
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
        if mode != OVERRIDE_AUTO and not self.runtime.execution_enabled:
            raise ValueError("Enable Automatic control before selecting a forced action")
        self._planning_revision += 1
        self.actuator.invalidate_pending_commands()
        self.runtime.override_action = mode
        await self.store.save(self.runtime)
        await self.async_request_refresh()

    async def async_force_safe(
        self, *, disable_automatic_control: bool = True
    ) -> None:
        if self._unloading:
            return
        self._planning_revision += 1
        self._safe_request_id += 1
        self.actuator.invalidate_pending_commands()
        self.runtime.pending_safe = True
        if disable_automatic_control:
            self.runtime.execution_enabled = False
        self.runtime.override_action = OVERRIDE_AUTO
        await self.store.save(self.runtime)
        await self._async_retry_safe(datetime.now(UTC))
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


def _valid_sizing_result(value: Any) -> bool:
    """Never publish malformed or non-finite restored financial estimates."""
    return isinstance(value, dict) and value.get("state") in {
        "learning", "bigger_battery_better", "size_is_fine", "battery_too_big",
    } and _finite_json(value)


def _finite_json(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_finite_json(item) for item in value)
    return value is None or isinstance(value, (str, int, bool))


def _control_number(controls: dict[str, str], key: str) -> float | None:
    try:
        value = float(controls[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None
