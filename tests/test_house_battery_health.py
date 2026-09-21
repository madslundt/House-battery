"""Health checks for direct-local battery entries."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import CONF_GRID_AVAILABLE, CONF_GRID_IMPORT_POWER
from house_battery.health import get_health_problems


class States(dict[str, SimpleNamespace]):
    """Minimal Home Assistant state registry facade."""

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return super().get(entity_id)


def test_direct_entry_accepts_stable_grid_availability_signal() -> None:
    """An on-grid binary sensor need not publish repeated on states."""
    now = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
    hass = SimpleNamespace(
        states=States(
            {
                "sensor.grid_import": SimpleNamespace(
                    state="500", last_updated=now - timedelta(minutes=1)
                ),
                "binary_sensor.grid_available": SimpleNamespace(
                    state="on", last_updated=now - timedelta(hours=1)
                ),
            }
        )
    )

    assert get_health_problems(
        hass,
        {
            "host": "192.168.30.90",
            CONF_GRID_IMPORT_POWER: "sensor.grid_import",
            CONF_GRID_AVAILABLE: "binary_sensor.grid_available",
        },
        now,
    ) == []
