"""Persistent runtime state for one optimizer entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.helpers.storage import Store

from .accounting import EnergyLedger
from .const import DEFAULT_SETTINGS, DOMAIN, STORAGE_KEY, STORAGE_VERSION
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
    forecast_accuracies: dict[str, ForecastAccuracy] = field(default_factory=dict)

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
        """Prune and count the rolling 24-hour transition budget."""
        cutoff = now - timedelta(hours=24)
        self.transitions = [
            value for value in self.transitions if _is_recent_timestamp(value, cutoff)
        ]
        return len(self.transitions)

    def active_transition_times(self, now: datetime) -> tuple[datetime, ...]:
        """Return valid transitions that still consume the rolling budget."""
        self.transitions_used(now)
        return tuple(
            timestamp
            for value in self.transitions
            if (timestamp := _parse_timestamp(value)) is not None
        )

    def collapse_rapid_transition_burst(
        self, now: datetime, *, maximum_transitions: int
    ) -> bool:
        """Recover transition counters inflated by pre-idempotency direct writes."""
        self.transitions_used(now)
        parsed = [
            (value, _parse_timestamp(value))
            for value in self.transitions
        ]
        parsed = [(value, timestamp) for value, timestamp in parsed if timestamp]
        if len(parsed) < maximum_transitions:
            return False
        latest = max(timestamp for _, timestamp in parsed)
        window = timedelta(minutes=self.settings["minimum_mode_minutes"])
        burst = [
            (value, timestamp)
            for value, timestamp in parsed
            if latest - timestamp <= window
        ]
        if len(burst) < maximum_transitions:
            return False
        burst_values = {value for value, _ in burst}
        first_value, _ = min(burst, key=lambda item: item[1])
        self.transitions = [
            value for value in self.transitions if value not in burst_values
        ] + [first_value]
        return True

    def mode_lock_remaining(self, now: datetime) -> int:
        """Return the remaining anti-chatter mode lock duration."""
        if not self.last_action_at:
            return 0
        try:
            elapsed = (
                now - datetime.fromisoformat(self.last_action_at)
            ).total_seconds()
        except ValueError:
            return 0
        return max(0, round(self.settings["minimum_mode_minutes"] - elapsed / 60))

    def as_dict(self) -> dict[str, Any]:
        return {
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
            "forecast_accuracies": {
                source: accuracy.as_dict()
                for source, accuracy in self.forecast_accuracies.items()
            },
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
        return cls(
            settings=settings,
            load_learner=LoadLearner.from_dict(data.get("load_learner", {})),
            load_learner_source=(
                data.get("load_learner_source")
                if isinstance(data.get("load_learner_source"), str)
                else None
            ),
            battery_learner=BatteryLearner.from_dict(data.get("battery_learner", {})),
            ledger=EnergyLedger.from_dict(data.get("ledger", {})),
            decisions=list(data.get("decisions", []))[-500:],
            scheduled_loads=list(data.get("scheduled_loads", [])),
            execution_enabled=bool(data.get("execution_enabled", False)),
            last_action=str(data.get("last_action", "safe")),
            last_action_at=data.get("last_action_at"),
            transitions=list(data.get("transitions", []))[-100:],
            forecast_enabled=bool(data.get("forecast_enabled", False)),
            forecast_accuracies=accuracies,
        )


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
