"""Persistent runtime state for one optimizer entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.helpers.storage import Store

from .accounting import EnergyLedger
from .const import DEFAULT_SETTINGS, DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .dailyplan import DailyPlan
from .forecast import ForecastAccuracy
from .learning import BatteryLearner, LoadLearner


@dataclass(slots=True)
class RuntimeState:
    """All state that must survive a Home Assistant restart."""

    settings: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    load_learner: LoadLearner = field(default_factory=LoadLearner)
    load_learner_source: str | None = None
    battery_learner: BatteryLearner = field(default_factory=BatteryLearner)
    ledger: EnergyLedger = field(default_factory=EnergyLedger)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    scheduled_loads: list[dict[str, Any]] = field(default_factory=list)
    execution_enabled: bool = False
    last_action: str = "safe"
    last_action_at: str | None = None
    transitions: list[str] = field(default_factory=list)
    forecast_enabled: bool = False
    opportunistic_charging_enabled: bool = False
    # Manual operating override. `auto` follows the optimizer plan; a forced
    # mode (charge/battery/grid) commands that action every refresh so the
    # physical functions can be verified independently of the price plan.
    override_action: str = "auto"
    forecast_accuracies: dict[str, ForecastAccuracy] = field(default_factory=dict)
    # ISO-8601 timestamp recorded when the schema-v2 power-flow model first
    # loaded this entry. Pre-v2 battery-learning and flow-accounting evidence
    # used the (incorrect) FBP "Discharge" telemetry, so those totals are
    # reset on migration while everything else is preserved.
    flow_model_started_at: str | None = None
    # Persisted, reconciled timeline for the current local calendar day.  The
    # optimizer only plans the future; this is what the dashboard shows as the
    # complete 00:00 -> 24:00 day.  It survives restarts and midnight and is
    # only ever updated by reconciling a fresh optimization on top of the
    # immutable published past.
    daily_plan: DailyPlan = field(default_factory=lambda: DailyPlan(date=""))

    def forecast_accuracy_for(self, source: str) -> ForecastAccuracy:
        """Return the independent accuracy history for one forecast entity."""
        return self.forecast_accuracies.setdefault(source, ForecastAccuracy())

    def migrate_legacy_forecast_accuracy(self, source: str) -> None:
        """Associate pre-multi-source evidence with its configured source."""
        legacy = self.forecast_accuracies.get("legacy")
        if legacy is not None and source not in self.forecast_accuracies:
            self.forecast_accuracies[source] = self.forecast_accuracies.pop("legacy")

    @property
    def forecast_accuracy(self) -> ForecastAccuracy:
        """Backward-compatible access to unassigned legacy accuracy evidence."""
        return self.forecast_accuracy_for("legacy")

    def transitions_used(self, now: datetime) -> int:
        """Prune and count recent action changes for diagnostics."""
        cutoff = now - timedelta(hours=24)
        self.transitions = [
            value for value in self.transitions if _is_recent_timestamp(value, cutoff)
        ]
        return len(self.transitions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "settings": self.settings,
            "load_learner": self.load_learner.as_dict(),
            "load_learner_source": self.load_learner_source,
            "battery_learner": self.battery_learner.as_dict(),
            "ledger": self.ledger.as_dict(),
            "decisions": self.decisions[-500:],
            "scheduled_loads": self.scheduled_loads,
            "execution_enabled": self.execution_enabled,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "transitions": self.transitions[-100:],
            "forecast_enabled": self.forecast_enabled,
            "opportunistic_charging_enabled": self.opportunistic_charging_enabled,
            "forecast_accuracies": {
                source: accuracy.as_dict()
                for source, accuracy in self.forecast_accuracies.items()
            },
            "daily_plan": self.daily_plan.as_dict(),
            "override_action": self.override_action,
            "flow_model_started_at": self.flow_model_started_at,
        }

    def export(self, entry_title: str, status: dict[str, Any]) -> dict[str, Any]:
        """Return a stable evidence bundle for diagnostics and offline analysis."""
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "entry": {"title": entry_title, "domain": DOMAIN},
            "settings": self.settings,
            "status": status,
            "load_model": self.load_learner.as_dict(),
            "battery_model": self.battery_learner.as_dict(),
            "ledger": self.ledger.as_dict(),
            "decisions": self.decisions,
            "scheduled_loads": self.scheduled_loads,
            "forecast": {
                "enabled": self.forecast_enabled,
                "accuracies": {
                    source: accuracy.as_dict()
                    for source, accuracy in self.forecast_accuracies.items()
                },
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeState:
        # Schema-v2 introduced the canonical load-vs-grid power-flow model. Any
        # payload produced before it (no schema_version, or an explicit 1) used
        # the incorrect FBP "Discharge" telemetry for both battery learning and
        # flow accounting, so those totals must be discarded on first load. Every
        # other field is preserved so the optimizer keeps its settings, forecast
        # evidence and manual mode preference across the upgrade.
        is_legacy = data.get("schema_version", 1) < 2
        settings = dict(DEFAULT_SETTINGS)
        settings.update(
            {
                key: float(value)
                for key, value in data.get("settings", {}).items()
                if key in settings
            }
        )
        accuracies: dict[str, ForecastAccuracy] = {}
        for source, accuracy in data.get("forecast_accuracies", {}).items():
            if isinstance(source, str) and isinstance(accuracy, dict):
                accuracies[source] = ForecastAccuracy.from_dict(accuracy)
        legacy_accuracy = data.get("forecast_accuracy")
        if not accuracies and isinstance(legacy_accuracy, dict):
            accuracies["legacy"] = ForecastAccuracy.from_dict(legacy_accuracy)
        state = cls(
            settings=settings,
            load_learner=LoadLearner.from_dict(data.get("load_learner", {})),
            load_learner_source=(
                data.get("load_learner_source")
                if isinstance(data.get("load_learner_source"), str)
                else None
            ),
            battery_learner=(
                BatteryLearner()
                if is_legacy
                else BatteryLearner.from_dict(data.get("battery_learner", {}))
            ),
            ledger=(
                EnergyLedger()
                if is_legacy
                else EnergyLedger.from_dict(data.get("ledger", {}))
            ),
            decisions=list(data.get("decisions", []))[-500:],
            scheduled_loads=list(data.get("scheduled_loads", [])),
            execution_enabled=bool(data.get("execution_enabled", False)),
            last_action=str(data.get("last_action", "safe")),
            last_action_at=data.get("last_action_at"),
            transitions=list(data.get("transitions", []))[-100:],
            forecast_enabled=bool(data.get("forecast_enabled", False)),
            opportunistic_charging_enabled=bool(
                data.get("opportunistic_charging_enabled", False)
            ),
            override_action=str(data.get("override_action", "auto") or "auto"),
            forecast_accuracies=accuracies,
            daily_plan=DailyPlan.from_dict(data.get("daily_plan", {})),
            flow_model_started_at=(
                data.get("flow_model_started_at")
                if isinstance(data.get("flow_model_started_at"), str)
                else None
            ),
        )
        # Stamp the moment the v2 flow model first loaded this entry. Legacy
        # payloads have no timestamp, so the first v2 load records "now"; a
        # v2 payload preserves the value it already carried.
        if not state.flow_model_started_at:
            state.flow_model_started_at = datetime.now(UTC).isoformat()
        return state


class RuntimeStore:
    """Small persistence adapter."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}"
        )

    async def load(self) -> RuntimeState:
        return RuntimeState.from_dict(await self._store.async_load() or {})

    async def save(self, state: RuntimeState) -> None:
        await self._store.async_save(state.as_dict())


def _is_recent_timestamp(value: str, cutoff: datetime) -> bool:
    timestamp = _parse_timestamp(value)
    return timestamp is not None and timestamp >= cutoff


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
