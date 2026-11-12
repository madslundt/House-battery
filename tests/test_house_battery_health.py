"""Health checks for direct-local battery entries."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_DEVICE_TYPE,
    CONF_ECOFLOW_AC_PORT_SWITCH,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_SOC,
    DEVICE_ECOFLOW,
)
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


def test_ecoflow_health_accepts_stable_binary_and_switch_states() -> None:
    now = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
    entity_ids = {
        CONF_SOC: "sensor.river_soc",
        CONF_LOAD_POWER: "sensor.river_ac_output",
        CONF_GRID_AVAILABLE: "binary_sensor.grid_available",
        CONF_BATTERY_CHARGE_POWER: "sensor.river_ac_input",
        CONF_BATTERY_DISCHARGE_POWER: "sensor.river_ac_output",
        CONF_ECOFLOW_AC_PORT_SWITCH: "switch.river_ac_port",
    }
    states = States({
        "sensor.river_soc": SimpleNamespace(state="50", last_updated=now),
        "sensor.river_ac_output": SimpleNamespace(state="120", last_updated=now),
        "binary_sensor.grid_available": SimpleNamespace(
            state="on", last_updated=now - timedelta(hours=1)
        ),
        "sensor.river_ac_input": SimpleNamespace(state="0", last_updated=now),
        "switch.river_ac_port": SimpleNamespace(
            state="on", last_updated=now - timedelta(hours=1)
        ),
    })

    assert get_health_problems(
        SimpleNamespace(states=states),
        {CONF_DEVICE_TYPE: DEVICE_ECOFLOW, **entity_ids},
        now,
    ) == []


def test_ecoflow_health_still_rejects_stale_numeric_telemetry() -> None:
    now = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
    entity_ids = {
        CONF_SOC: "sensor.river_soc",
        CONF_LOAD_POWER: "sensor.river_ac_output",
        CONF_GRID_AVAILABLE: "binary_sensor.grid_available",
        CONF_BATTERY_CHARGE_POWER: "sensor.river_ac_input",
        CONF_BATTERY_DISCHARGE_POWER: "sensor.river_ac_output",
        CONF_ECOFLOW_AC_PORT_SWITCH: "switch.river_ac_port",
    }
    states = States({
        "sensor.river_soc": SimpleNamespace(state="50", last_updated=now),
        "sensor.river_ac_output": SimpleNamespace(
            state="120", last_updated=now - timedelta(minutes=10)
        ),
        "binary_sensor.grid_available": SimpleNamespace(state="on", last_updated=now),
        "sensor.river_ac_input": SimpleNamespace(state="0", last_updated=now),
        "switch.river_ac_port": SimpleNamespace(state="on", last_updated=now),
    })

    problems = get_health_problems(
        SimpleNamespace(states=states),
        {CONF_DEVICE_TYPE: DEVICE_ECOFLOW, **entity_ids},
        now,
    )

    assert problems == ["sensor.river_ac_output stale"]
