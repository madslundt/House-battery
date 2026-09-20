"""Observable optimizer, economy, learning, and battery sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
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
        key="pv_power_w",
        name="PV input power",
        icon="mdi:solar-power",
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
        key="month_net_savings_dkk",
        name="Estimated realized savings this month",
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
        key="month_discharge_kwh",
        name="Battery discharge this month",
        icon="mdi:battery-arrow-down",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
    ),
    FbpSensorDescription(
        key="lifetime_discharge_kwh",
        name="Battery discharge total",
        icon="mdi:battery-sync",
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        precision=3,
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
            FbpStoragePolicySensor(coordinator),
            FbpBatteryLearningSensor(coordinator),
            FbpDecisionHistorySensor(coordinator),
            *(FbpValueSensor(coordinator, description) for description in SENSORS),
        ]
    )


def _plan_blocks(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not plan:
        return []
    blocks: list[dict[str, Any]] = []
    for slot in plan.get("slots", []):
        if (
            blocks
            and blocks[-1]["action"] == slot["action"]
            and blocks[-1]["end"] == slot["start"]
        ):
            blocks[-1]["end"] = slot["end"]
            blocks[-1]["expected_savings_dkk"] += (
                slot["baseline_cost_dkk"] - slot["interval_cost_dkk"]
            )
            blocks[-1]["energy_kwh"] += (
                slot["battery_charge_wh"] + slot["battery_discharge_wh"]
            ) / 1000
            blocks[-1]["soc_end"] = slot["soc_end"]
        else:
            blocks.append(
                {
                    "start": slot["start"],
                    "end": slot["end"],
                    "action": slot["action"],
                    "expected_savings_dkk": slot["baseline_cost_dkk"]
                    - slot["interval_cost_dkk"],
                    "energy_kwh": (
                        slot["battery_charge_wh"] + slot["battery_discharge_wh"]
                    )
                    / 1000,
                    "soc_start": slot["soc_start"],
                    "soc_end": slot["soc_end"],
                    "reason": slot["reason"],
                }
            )
    for block in blocks:
        for key in ("expected_savings_dkk", "energy_kwh", "soc_start", "soc_end"):
            block[key] = round(block[key], 3)
    return blocks


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
            "observed_action": self.coordinator.data.get("observed_action"),
            "command_result": self.coordinator.data.get("command_result"),
        }


class FbpModeSensor(Fbp1200Entity, SensorEntity):
    _attr_name = "Battery mode"
    _attr_icon = "mdi:battery-sync"

    def __init__(self, coordinator: Fbp1200Coordinator) -> None:
        super().__init__(coordinator, "battery_mode")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.get("observed_action", Action.SAFE.value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source": self.coordinator.config.get("operating_mode_entity"),
            "current_decision": self.coordinator.data.get("current_action"),
            "note": "Read-back from the local Operating Mode entity; charge and discharge power sensors show the physical result.",
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
        plan = self.coordinator.data.get("plan")
        return {
            "blocks": _plan_blocks(plan),
            "horizon_slots": len(plan.get("slots", [])) if plan else 0,
            "expected_cost_dkk": self.coordinator.data.get("expected_cost_dkk"),
            "baseline_cost_dkk": self.coordinator.data.get("baseline_cost_dkk"),
            "expected_savings_dkk": self.coordinator.data.get("expected_savings_dkk"),
            "terminal_price_dkk_per_kwh": self.coordinator.data.get(
                "terminal_price_dkk_per_kwh"
            ),
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
        self._attr_state_class = description.state_class

    @property
    def native_value(self) -> Any:
        for period in ("today", "month"):
            prefix = f"{period}_"
            if self.description.key.startswith(prefix):
                return self.coordinator.data.get(period, {}).get(
                    self.description.key.removeprefix(prefix)
                )
        return self.coordinator.data.get(self.description.key)
