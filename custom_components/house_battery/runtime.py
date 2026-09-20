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
    battery_learner: BatteryLearner = field(default_factory=BatteryLearner)
    ledger: EnergyLedger = field(default_factory=EnergyLedger)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    scheduled_loads: list[dict[str, Any]] = field(default_factory=list)
    execution_enabled: bool = False
    last_action: str = "safe"
    last_action_at: str | None = None
    transitions: list[str] = field(default_factory=list)
    forecast_enabled: bool = False
    forecast_accuracy: ForecastAccuracy = field(default_factory=ForecastAccuracy)

    def transitions_used(self, now: datetime) -> int:
        """Prune and count the rolling 24-hour transition budget."""
        cutoff = now - timedelta(hours=24)
        self.transitions = [
            value for value in self.transitions if _is_recent_timestamp(value, cutoff)
        ]
        return len(self.transitions)

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
            "battery_learner": self.battery_learner.as_dict(),
            "ledger": self.ledger.as_dict(),
            "decisions": self.decisions[-500:],
            "scheduled_loads": self.scheduled_loads,
            "execution_enabled": self.execution_enabled,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "transitions": self.transitions[-100:],
            "forecast_enabled": self.forecast_enabled,
            "forecast_accuracy": self.forecast_accuracy.as_dict(),
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
                "accuracy": self.forecast_accuracy.as_dict(),
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
        return cls(
            settings=settings,
            load_learner=LoadLearner.from_dict(data.get("load_learner", {})),
            battery_learner=BatteryLearner.from_dict(data.get("battery_learner", {})),
            ledger=EnergyLedger.from_dict(data.get("ledger", {})),
            decisions=list(data.get("decisions", []))[-500:],
            scheduled_loads=list(data.get("scheduled_loads", [])),
            execution_enabled=bool(data.get("execution_enabled", False)),
            last_action=str(data.get("last_action", "safe")),
            last_action_at=data.get("last_action_at"),
            transitions=list(data.get("transitions", []))[-100:],
            forecast_enabled=bool(data.get("forecast_enabled", False)),
            forecast_accuracy=ForecastAccuracy.from_dict(
                data.get("forecast_accuracy", {})
            ),
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
    try:
        return datetime.fromisoformat(value) >= cutoff
    except (TypeError, ValueError):
        return False
