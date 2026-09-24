"""Observable optimizer, economy, learning, and battery sensors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import Fbp1200Coordinator
from .entity import Fbp1200Entity
from .models import Action


@dataclass(frozen=True, kw_only=True)
class FbpSensorDescription:
    key: str
    name: str
    icon: str
    unit: str | None = None
    precision: int | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None


SENSORS = (
    FbpSensorDescription(
        key="soc",
        name="Battery state of charge",
        icon="mdi:battery",
        unit=PERCENTAGE,
        precision=1,
    ),
    FbpSensorDescription(
        key="load_power_w",
        name="Connected load power",
        icon="mdi:home-lightning-bolt",
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    FbpSensorDescription(
        key="grid_import_power_w",
        name="Grid import power",
        icon="mdi:transmission-tower-import",
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    FbpSensorDescription(
        key="grid_export_power_w",
        name="Grid export power",
        icon="mdi:transmission-tower-export",
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    FbpSensorDescription(
        key="battery_charge_power_w",
        name="Battery charge power",
        icon="mdi:battery-plus",
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    FbpSensorDescription(
        key="battery_discharge_power_w",
        name="Battery discharge power",
        icon="mdi:battery-minus",
        unit=UnitOfPower.WATT,
        precision=0,
    ),
    FbpSensorDescription(
        key="current_price_dkk_per_kwh",
        name="Current electricity price",
        icon="mdi:cash-clock",
        unit="DKK/kWh",
        precision=3,
    ),
    FbpSensorDescription(
        key="effective_target_soc",
        name="Effective charge target SOC",
        icon="mdi:battery-charging-100",
        unit=PERCENTAGE,
        precision=1,
    ),
    FbpSensorDescription(
        key="price_spread_dkk_per_kwh",
        name="Known price spread",
        icon="mdi:chart-timeline-variant-shimmer",
        unit="DKK/kWh",
        precision=3,
    ),
    FbpSensorDescription(
        key="effective_margin_dkk_per_kwh",
        name="Best effective price margin",
        icon="mdi:cash-plus",
        unit="DKK/kWh",
        precision=3,
    ),
    FbpSensorDescription(
        key="expected_savings_dkk",
        name="Expected plan savings",
        icon="mdi:cash-plus",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="today_net_savings_dkk",
        name="Estimated realized savings today",
        icon="mdi:cash-check",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="yesterday_net_savings_dkk",
        name="Estimated realized savings yesterday",
        icon="mdi:cash-check",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="week_net_savings_dkk",
        name="Estimated realized savings this week",
        icon="mdi:cash-check",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="last_week_net_savings_dkk",
        name="Estimated realized savings last week",
        icon="mdi:cash-check",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="month_net_savings_dkk",
        name="Estimated realized savings this month",
        icon="mdi:calendar-check",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="last_month_net_savings_dkk",
        name="Estimated realized savings last month",
        icon="mdi:calendar-check",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="lifetime_net_savings_dkk",
        name="Estimated realized savings total",
        icon="mdi:chart-line",
        unit="DKK",
        precision=2,
    ),
    FbpSensorDescription(
        key="today_charge_kwh",
        name="Battery charge today",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="yesterday_charge_kwh",
        name="Battery charge yesterday",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="week_charge_kwh",
        name="Battery charge this week",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="last_week_charge_kwh",
        name="Battery charge last week",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="today_discharge_kwh",
        name="Battery discharge today",
        icon="mdi:battery-arrow-down",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="month_charge_kwh",
        name="Battery charge this month",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="last_month_charge_kwh",
        name="Battery charge last month",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="month_discharge_kwh",
        name="Battery discharge this month",
        icon="mdi:battery-arrow-down",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="lifetime_charge_kwh",
        name="Battery charge total",
        icon="mdi:battery-arrow-up",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FbpSensorDescription(
        key="lifetime_discharge_kwh",
        name="Battery discharge total",
        icon="mdi:battery-sync",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FbpSensorDescription(
        key="equivalent_full_cycles",
        name="Equivalent full cycles",
        icon="mdi:sync",
        unit="cycles",
        precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FbpSensorDescription(
        key="estimated_degradation_pct",
        name="Estimated battery degradation",
        icon="mdi:battery-heart-variant",
        unit=PERCENTAGE,
        precision=3,
    ),
    FbpSensorDescription(
        key="estimated_remaining_capacity_pct",
        name="Estimated remaining capacity",
        icon="mdi:battery-heart",
        unit=PERCENTAGE,
        precision=2,
    ),
    FbpSensorDescription(
        key="learned_capacity_kwh",
        name="Learned usable capacity",
        icon="mdi:battery-high",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="learned_efficiency_pct",
        name="Learned round-trip efficiency",
        icon="mdi:percent-circle",
        unit=PERCENTAGE,
        precision=1,
    ),
    FbpSensorDescription(
        key="load_learning_confidence_pct",
        name="Load learning coverage",
        icon="mdi:brain",
        unit=PERCENTAGE,
        precision=1,
    ),
    FbpSensorDescription(
        key="load_forecast_error_w",
        name="Load forecast mean absolute error",
        icon="mdi:chart-bell-curve",
        unit=UnitOfPower.WATT,
        precision=0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    async_add_entities(
        [
            FbpSystemStateSensor(coordinator),
            FbpActionSensor(coordinator),
            FbpModeSensor(coordinator),
            FbpPlanSensor(coordinator),
            FbpCurrentPlanSlotSensor(coordinator),
            FbpPlanExecutionSensor(coordinator),
            FbpPlannedLoadPowerSensor(coordinator),
            FbpStoragePolicySensor(coordinator),
            FbpBatteryLearningSensor(coordinator),
            FbpPriceForecastAccuracySensor(coordinator),
            FbpDecisionHistorySensor(coordinator),
            *(
                [FbpLocalLoadDiagnosticsSensor(coordinator)]
                if coordinator.is_direct_local
                else []
            ),
            *(FbpValueSensor(coordinator, description) for description in SENSORS),
        ]
    )


def daily_plan_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the complete-day operating blocks from the persisted daily plan.

    The daily plan is the authoritative 00:00 -> 24:00 timeline: the published
    past before the last replan is immutable and the future is the latest
    optimization, so the dashboard always shows the whole current day instead of
    restarting at the current interval.
    """
    plan = data.get("daily_plan")
    if not isinstance(plan, dict):
        return []
    # ``actual_soc`` is already attached to the first block by
    # ``DailyPlan.view_dict``; the daily plan itself only ever appends the
    # observed curve, never rewrites the published pre-replan history.
    return list(plan.get("blocks", []))


class FbpSystemStateSensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Optimizer state"
    _attr_icon = "mdi:shield-battery"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "system_state")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.get("system_state", "BOOTSTRAP")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        keys = (
            "reason",
            "healthy",
            "execution_enabled",
            "commissioned",
            "command_result",
            "health_problems",
            "last_refresh",
        )
        return {key: self.coordinator.data.get(key) for key in keys}


class FbpActionSensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Current decision"
    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "current_action")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.get("current_action", Action.SAFE.value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "reason": self.coordinator.data.get("reason"),
            "configured_action": self.coordinator.data.get("observed_action"),
            "battery_activity": _battery_activity(self.coordinator.data),
            "command_result": self.coordinator.data.get("command_result"),
        }


def _battery_activity(data: dict[str, Any]) -> str:
    """Classify physical battery power, never a configured operating mode."""
    charge = float(data.get("battery_charge_power_w") or 0)
    discharge = float(data.get("battery_discharge_power_w") or 0)
    threshold_w = 10
    if charge >= threshold_w and discharge >= threshold_w:
        return "conflict"
    if charge >= threshold_w:
        return "charging"
    if discharge >= threshold_w:
        return "discharging"
    return "idle"


class FbpModeSensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Battery activity"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "battery_mode")

    @property
    def native_value(self) -> str:
        return _battery_activity(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source": "battery charge/discharge power telemetry",
            "configured_mode": self.coordinator.data.get("local_observed_mode"),
            "configured_action": self.coordinator.data.get("observed_action"),
            "current_decision": self.coordinator.data.get("current_action"),
            "charge_power_w": self.coordinator.data.get("battery_charge_power_w"),
            "discharge_power_w": self.coordinator.data.get("battery_discharge_power_w"),
            "active_power_threshold_w": 10,
            "note": "Physical activity is idle below 10 W. Configured mode is shown separately and does not prove energy movement.",
        }


class FbpPlanSensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Operation plan"
    _attr_icon = "mdi:timeline-clock-outline"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "operation_plan")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("plan_created_at")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # The complete-day timeline is the persisted daily plan (00:00 -> 24:00);
        # the full-horizon plan dict remains available on the other plan sensors
        # for the current actionable slot and horizon economics.
        plan = self.coordinator.data.get("daily_plan")
        return {
            "blocks": daily_plan_blocks(self.coordinator.data),
            "date": plan.get("date") if plan else None,
            "created_at": plan.get("created_at") if plan else None,
            "actual_soc": plan.get("actual_soc") if plan else None,
            "horizon_slots": plan.get("horizon_slots", 0) if plan else 0,
            "expected_cost_dkk": plan.get("expected_cost_dkk") if plan else None,
            "baseline_cost_dkk": plan.get("baseline_cost_dkk") if plan else None,
            "expected_savings_dkk": plan.get("expected_savings_dkk") if plan else None,
            "terminal_price_dkk_per_kwh": plan.get(
                "terminal_price_dkk_per_kwh"
            )
            if plan
            else None,
        }


def _current_plan_slot(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the executable current slot, if the planner has one."""
    plan = data.get("plan")
    if not isinstance(plan, dict):
        return None
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        return None
    slot = slots[0]
    return slot if isinstance(slot, dict) else None


class FbpCurrentPlanSlotSensor(Fbp1200Entity, SensorEntity):
    """Expose a compact, recorder-friendly copy of the active planned slot."""

    _attr_name = "Current plan slot"
    _attr_icon = "mdi:timeline-clock"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "current_plan_slot")

    @property
    def native_value(self) -> str | None:
        slot = _current_plan_slot(self.coordinator.data)
        return slot.get("start") if slot else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = _current_plan_slot(self.coordinator.data)
        if not slot:
            return {"status": "no_executable_plan"}
        hours = max(
            0.0,
            (
                datetime.fromisoformat(slot["end"])
                - datetime.fromisoformat(slot["start"])
            ).total_seconds()
            / 3600,
        )
        expected_load_wh = float(slot.get("expected_load_wh") or 0)
        return {
            "status": "planned",
            "end": slot.get("end"),
            "action": slot.get("action"),
            "price_dkk_per_kwh": slot.get("price"),
            "price_source": slot.get("price_source", "known"),
            "price_uncertainty_dkk_per_kwh": slot.get(
                "price_uncertainty_dkk_per_kwh", 0
            ),
            "expected_load_kwh": round(expected_load_wh / 1000, 4),
            "expected_average_load_w": round(expected_load_wh / hours, 1)
            if hours
            else None,
            "planned_grid_import_kwh": round(
                float(slot.get("grid_import_wh") or 0) / 1000, 4
            ),
            "planned_battery_charge_kwh": round(
                float(slot.get("battery_charge_wh") or 0) / 1000, 4
            ),
            "planned_battery_discharge_kwh": round(
                float(slot.get("battery_discharge_wh") or 0) / 1000, 4
            ),
            "soc_start": slot.get("soc_start"),
            "soc_end": slot.get("soc_end"),
            "expected_cost_dkk": slot.get("interval_cost_dkk"),
            "baseline_cost_dkk": slot.get("baseline_cost_dkk"),
            "reason": slot.get("reason"),
        }


class FbpPlannedLoadPowerSensor(Fbp1200Entity, SensorEntity):
    """Expose the active-slot load forecast as a time-series friendly value."""

    _attr_name = "Planned load power"
    _attr_icon = "mdi:home-clock-outline"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "planned_load_power")

    @property
    def native_value(self) -> float | None:
        slot = _current_plan_slot(self.coordinator.data)
        if not slot:
            return None
        try:
            hours = (
                datetime.fromisoformat(slot["end"])
                - datetime.fromisoformat(slot["start"])
            ).total_seconds() / 3600
            return round(float(slot["expected_load_wh"]) / hours)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = _current_plan_slot(self.coordinator.data)
        return {
            "slot_start": slot.get("start") if slot else None,
            "slot_end": slot.get("end") if slot else None,
            "includes_scheduled_loads": True,
            "note": "Average connected-load forecast for the active plan slot.",
        }


class FbpPlanExecutionSensor(Fbp1200Entity, SensorEntity):
    """Compare expected physical battery movement with live telemetry."""

    _attr_name = "Plan execution"
    _attr_icon = "mdi:clipboard-check-outline"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "plan_execution")

    @property
    def native_value(self) -> str:
        slot = _current_plan_slot(self.coordinator.data)
        if not slot:
            return "not_assessable"
        charge_wh = float(slot.get("battery_charge_wh") or 0)
        discharge_wh = float(slot.get("battery_discharge_wh") or 0)
        expected = (
            "charging"
            if charge_wh > 0
            else "discharging" if discharge_wh > 0 else "idle"
        )
        actual = _battery_activity(self.coordinator.data)
        if expected == actual:
            return "matching"
        if expected == "idle":
            return "unexpected_battery_activity"
        return "battery_not_moving_as_planned"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = _current_plan_slot(self.coordinator.data)
        if not slot:
            return {"status": "no_executable_plan"}
        charge_wh = float(slot.get("battery_charge_wh") or 0)
        discharge_wh = float(slot.get("battery_discharge_wh") or 0)
        expected = (
            "charging"
            if charge_wh > 0
            else "discharging" if discharge_wh > 0 else "idle"
        )
        return {
            "expected_battery_activity": expected,
            "actual_battery_activity": _battery_activity(self.coordinator.data),
            "planned_action": slot.get("action"),
            "planned_battery_charge_kwh": round(charge_wh / 1000, 4),
            "planned_battery_discharge_kwh": round(discharge_wh / 1000, 4),
            "actual_charge_power_w": self.coordinator.data.get("battery_charge_power_w"),
            "actual_discharge_power_w": self.coordinator.data.get(
                "battery_discharge_power_w"
            ),
            "command_result": self.coordinator.data.get("command_result"),
            "note": "A live mismatch is a diagnostic signal, not proof of a failed plan; assess it over the complete slot.",
        }


class FbpStoragePolicySensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Extra storage policy"
    _attr_icon = "mdi:battery-plus-outline"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "extra_storage_policy")

    @property
    def native_value(self) -> str:
        return (
            "active" if self.coordinator.data.get("extra_storage_active") else "normal"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        keys = (
            "effective_target_soc",
            "price_spread_dkk_per_kwh",
            "effective_margin_dkk_per_kwh",
            "extra_storage_known_slot_count",
            "extra_storage_charge_price_dkk_per_kwh",
            "extra_storage_discharge_price_dkk_per_kwh",
            "extra_storage_reason",
        )
        return {key: self.coordinator.data.get(key) for key in keys}


class FbpBatteryLearningSensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Battery learning"
    _attr_icon = "mdi:battery-heart-outline"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "battery_learning")

    @property
    def native_value(self) -> str:
        return (
            "ready"
            if self.coordinator.data.get("capacity_learning_ready")
            and self.coordinator.data.get("efficiency_learning_ready")
            else "learning"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        keys = (
            "capacity_learning_samples",
            "capacity_learning_ready",
            "efficiency_learning_samples",
            "efficiency_learning_ready",
            "learned_capacity_kwh",
            "learned_efficiency_pct",
        )
        return {key: self.coordinator.data.get(key) for key in keys}


class FbpPriceForecastAccuracySensor(Fbp1200Entity, SensorEntity):
    """Show whether the external price forecast has been accurate in practice."""

    _attr_name = "External price forecast accuracy"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "external_price_forecast_accuracy")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.get("price_forecast_accuracy", "unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        keys = (
            "price_forecast_enabled",
            "price_forecast_source",
            "price_forecast_planning_source",
            "price_forecast_sources",
            "price_forecast_status",
            "price_forecast_last_updated",
            "price_forecast_available_slots",
            "price_forecast_used_slots",
            "price_forecast_samples",
            "price_forecast_mae_dkk_per_kwh",
            "price_forecast_bias_dkk_per_kwh",
            "price_forecast_within_uncertainty_pct",
        )
        return {key: self.coordinator.data.get(key) for key in keys} | {
            "meaning": "Each configured source is scored independently. MAE is the average absolute forecast error; bias is forecast minus actual, so positive means over-prediction.",
            "uncertainty_setting": "External price forecast uncertainty number",
        }


class FbpDecisionHistorySensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Decision history"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "decision_history")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.runtime.decisions)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"recent": self.coordinator.data.get("recent_decisions", [])}


class FbpLocalLoadDiagnosticsSensor(Fbp1200Entity, SensorEntity):
    """Expose local load scopes without feeding them into control yet."""

    _attr_name = "Local load diagnostics"
    _attr_icon = "mdi:meter-electric-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "local_load_diagnostics")

    @property
    def native_value(self) -> str:
        diagnostics = self.coordinator.data.get("local_load_diagnostics")
        return (
            "ready"
            if diagnostics
            and any(
                isinstance(diagnostics.get(key), (int, float))
                for key in (
                    "smart_load_power_w",
                    "backup_load_power_w",
                    "off_grid_load_power_total_w",
                )
            )
            else "unavailable"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **(self.coordinator.data.get("local_load_diagnostics") or {}),
            "selection_status": (
                "Uses the complete per-storage off-grid total as the battery-served "
                "load; incomplete local frames fail closed."
            ),
            "field_meanings": {
                "meter_total_active_power_w": "Whole-site meter total; never a battery-load candidate.",
                "smart_load_power_w": "FOSSiBOT smart-load total.",
                "backup_load_power_w": "FOSSiBOT backup/off-grid output total.",
                "off_grid_load_power_per_unit_w": "Per-storage off-grid readings in Storage_list order; null means that unit did not report it.",
                "off_grid_load_power_total_w": "Sum of every per-storage reading, available only when every unit reported one.",
            },
        }


class FbpValueSensor(Fbp1200Entity, SensorEntity):
    def __init__(
        self, coordinator: Fbp1200Coordinator, description: FbpSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_native_unit_of_measurement = description.unit
        self._attr_suggested_display_precision = description.precision
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class

    @property
    def native_value(self) -> Any:
        for period in (
            "today",
            "yesterday",
            "week",
            "last_week",
            "month",
            "last_month",
        ):
            prefix = f"{period}_"
            if self.description.key.startswith(prefix):
                return self.coordinator.data.get(period, {}).get(
                    self.description.key.removeprefix(prefix)
                )
        return self.coordinator.data.get(self.description.key)
