"""Tests for the manual storage override (auto / charge / battery / grid)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import (
    ACTION_BATTERY,
    ACTION_CHARGE,
    ACTION_GRID,
    OVERRIDE_AUTO,
    OVERRIDE_BATTERY,
    OVERRIDE_CHARGE,
    OVERRIDE_GRID,
    OVERRIDE_OPTIONS,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.models import Action
from house_battery.runtime import RuntimeState
from house_battery.select import FbpStorageOverrideSelect


class Entry:
    """Minimal ConfigEntry contract needed by DataUpdateCoordinator."""

    entry_id = "override-test"
    title = "FBP1200"

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.options: dict[str, object] = {}

    def async_on_unload(self, callback: object) -> None:
        del callback


class StaticStore:
    """Persistence seam used to verify restart round-trips."""

    def __init__(self, state: RuntimeState) -> None:
        self.state = state
        self.saved = False

    async def load(self) -> RuntimeState:
        return self.state

    async def save(self, state: RuntimeState) -> None:
        self.state = state
        self.saved = True


def make_coordinator(**options: object) -> Fbp1200Coordinator:
    """Build a coordinator inside a fresh event loop (HomeAssistant needs one)."""

    async def _build() -> Fbp1200Coordinator:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        data = {"commissioned": True, **options}
        coordinator = Fbp1200Coordinator(hass, Entry(data))
        coordinator.async_request_refresh = _no_refresh
        return coordinator

    return asyncio.run(_build())


def _coordinator_entry(**options: object) -> Fbp1200Coordinator:
    async def _build() -> Fbp1200Coordinator:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(hass, Entry(options))
        coordinator.async_request_refresh = _no_refresh
        return coordinator

    return asyncio.run(_build())


def test_override_action_maps_manual_modes_to_planner_actions() -> None:
    coordinator = make_coordinator()
    for mode, expected in (
        (OVERRIDE_CHARGE, ACTION_CHARGE),
        (OVERRIDE_BATTERY, ACTION_BATTERY),
        (OVERRIDE_GRID, ACTION_GRID),
    ):
        coordinator.runtime.override_action = mode
        assert coordinator._override_action() is Action(expected)


def test_override_action_is_none_for_auto() -> None:
    coordinator = make_coordinator()
    coordinator.runtime.override_action = OVERRIDE_AUTO
    assert coordinator._override_action() is None


def test_set_override_action_persists_and_requests_refresh() -> None:
    coordinator = make_coordinator()
    store = StaticStore(RuntimeState())
    coordinator.runtime.override_action = OVERRIDE_AUTO
    coordinator.store = store

    asyncio.run(coordinator.async_set_override_action(OVERRIDE_BATTERY))

    assert coordinator.runtime.override_action == OVERRIDE_BATTERY
    assert coordinator.runtime.override_action != OVERRIDE_AUTO
    assert store.saved is True


def test_set_override_action_rejects_unknown_mode() -> None:
    coordinator = make_coordinator()
    coordinator.runtime.override_action = OVERRIDE_AUTO

    with pytest.raises(ValueError, match="Unsupported operating override"):
        asyncio.run(coordinator.async_set_override_action("discharge"))

    # A rejected override must not mutate state.
    assert coordinator.runtime.override_action == OVERRIDE_AUTO


def test_set_override_action_requires_commissioning_for_forced_modes() -> None:
    coordinator = _coordinator_entry(commissioned=False)
    coordinator.store = StaticStore(RuntimeState())

    with pytest.raises(ValueError, match="Commission the integration"):
        asyncio.run(coordinator.async_set_override_action(OVERRIDE_CHARGE))

    assert coordinator.runtime.override_action == OVERRIDE_AUTO


def test_override_action_survives_a_restart() -> None:
    runtime = RuntimeState(override_action=OVERRIDE_BATTERY)
    restored = RuntimeState.from_dict(runtime.as_dict())
    assert restored.override_action == OVERRIDE_BATTERY
    assert restored.override_action != OVERRIDE_AUTO


def test_override_is_exposed_in_the_status_payload() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(hass, Entry({"commissioned": True}))
        coordinator.store = StaticStore(RuntimeState())
        coordinator.async_request_refresh = _no_refresh
        coordinator.runtime.override_action = OVERRIDE_GRID
        hass.states.async_set("sensor.grid_import", "500")
        hass.states.async_set("binary_sensor.grid_available", "on")

        status = await coordinator._async_update_data()

        assert status["mode_override"] == OVERRIDE_GRID

    asyncio.run(scenario())


def test_set_override_action_auto_resets_to_follow_the_plan() -> None:
    coordinator = make_coordinator()
    coordinator.runtime.override_action = OVERRIDE_BATTERY
    coordinator.store = StaticStore(RuntimeState())

    asyncio.run(coordinator.async_set_override_action(OVERRIDE_AUTO))

    assert coordinator.runtime.override_action == OVERRIDE_AUTO


async def _no_refresh() -> None:
    """Prevent update scheduling; these tests target the override seam only."""


def test_override_select_exposes_the_four_modes_and_current_choice() -> None:
    coordinator = make_coordinator()
    coordinator.runtime.override_action = OVERRIDE_BATTERY
    select = FbpStorageOverrideSelect(coordinator)

    assert select.options == list(OVERRIDE_OPTIONS)
    assert set(OVERRIDE_OPTIONS) == {OVERRIDE_AUTO, "charge", "battery", "grid"}
    assert select.current_option == OVERRIDE_BATTERY


def test_override_select_commands_the_coordinator() -> None:
    coordinator = make_coordinator()
    coordinator.runtime.override_action = OVERRIDE_AUTO
    coordinator.store = StaticStore(RuntimeState())
    select = FbpStorageOverrideSelect(coordinator)

    asyncio.run(select.async_select_option(OVERRIDE_GRID))

    assert coordinator.runtime.override_action == OVERRIDE_GRID
