"""Safety-boundary tests for local FBP1200 control."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.actuator import LocalControlAdapter
from house_battery.config_flow import _direct_schema, _schema
from house_battery.const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_COMMISSIONED,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_SOC,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.local_tcp import FbpLocalSnapshot
from house_battery.models import Action
from house_battery.runtime import RuntimeState


class FakeStates:
    """Small HA-state facade with number bounds."""

    def __init__(self, values: dict[str, SimpleNamespace]) -> None:
        self.values = values

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self.values.get(entity_id)


class FakeServices:
    """Service facade that can deliberately leave state read-back stale."""

    def __init__(self, states: FakeStates, *, apply_updates: bool = True) -> None:
        self.states = states
        self.apply_updates = apply_updates
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def async_call(
        self, domain: str, service: str, data: dict[str, object], **kwargs: object
    ) -> None:
        self.calls.append((domain, service, data))
        if not self.apply_updates:
            return
        state = self.states.get(str(data["entity_id"]))
        if state is None:
            return
        if domain == "number":
            state.state = str(data["value"])
        elif domain == "select":
            state.state = str(data["option"])


class FakeHass:
    """Only the HA APIs exercised by the adapter seam."""

    def __init__(self, states: FakeStates, services: FakeServices) -> None:
        self.states = states
        self.services = services
        self.flushes = 0

    async def async_block_till_done(self) -> None:
        self.flushes += 1


class Entry:
    """Minimal ConfigEntry contract needed by DataUpdateCoordinator."""

    entry_id = "control-test"
    title = "FBP1200"

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.options: dict[str, object] = {}

    def async_on_unload(self, callback: object) -> None:
        del callback


class StaticStore:
    """Persistence seam used to verify restart fail-closed behavior."""

    def __init__(self, state: RuntimeState) -> None:
        self.state = state
        self.saved = False

    async def load(self) -> RuntimeState:
        return self.state

    async def save(self, state: RuntimeState) -> None:
        self.state = state
        self.saved = True


def number(value: float, minimum: float = 0, maximum: float = 100) -> SimpleNamespace:
    return SimpleNamespace(
        state=str(value), attributes={"min": minimum, "max": maximum, "step": 1}
    )


def config() -> dict[str, object]:
    return {
        CONF_OPERATING_MODE: "select.fbp_mode",
        CONF_MIN_SOC_CONTROL: "number.fbp_min_soc",
        CONF_MAX_SOC_CONTROL: "number.fbp_max_soc",
        "commissioned": True,
    }


def test_options_require_native_soc_controls_before_commissioning() -> None:
    options = {
        CONF_SOC: "sensor.soc",
        CONF_LOAD_POWER: "sensor.load",
        CONF_GRID_IMPORT_POWER: "sensor.grid_import",
        CONF_GRID_AVAILABLE: "binary_sensor.grid_available",
        CONF_OPERATING_MODE: "select.fbp_mode",
        CONF_PRICE_ENTITIES: ["sensor.price"],
        CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
        CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
        CONF_MIN_SOC_CONTROL: "number.fbp_min_soc",
        CONF_MAX_SOC_CONTROL: "number.fbp_max_soc",
        CONF_COMMISSIONED: False,
    }

    assert _schema({}, options=True)(options) == options
    incomplete = dict(options)
    incomplete.pop(CONF_MAX_SOC_CONTROL)
    with pytest.raises(vol.Invalid):
        _schema({}, options=True)(incomplete)


def test_direct_load_requires_explicit_local_source_confirmation() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        coordinator = Fbp1200Coordinator(
            HomeAssistant("/tmp"), Entry({"host": "192.168.30.90"})
        )
        coordinator._local_snapshot = FbpLocalSnapshot(
            50,
            0,
            800,
            1907,
            {"SSumInfoList": [{"TotalSmartLoadElectricalPower": 106}]},
        )

        assert coordinator._load_power() is None
        assert "not selected" in (coordinator._direct_load_problem() or "")

        coordinator.entry.options = {
            "direct_load_source": "smart_load",
            "direct_load_confirmed": True,
            "direct_load_confirmed_source": "smart_load",
        }
        assert coordinator._load_power() == 106
        assert coordinator._direct_load_problem() is None

    asyncio.run(scenario())


def test_direct_setup_requires_only_grid_and_price_sources() -> None:
    direct_sources = {
        CONF_GRID_IMPORT_POWER: "sensor.watts_live_effekt",
        CONF_GRID_AVAILABLE: "binary_sensor.watts_grid_available",
        CONF_PRICE_ENTITIES: ["sensor.stromligning_current_price_vat"],
    }

    assert _direct_schema({})(direct_sources) == direct_sources


def runtime() -> RuntimeState:
    state = RuntimeState()
    state.settings.update({"absolute_min_soc": 10, "target_soc": 90})
    return state


def adapter(
    *, apply_updates: bool = True, maximum: float = 100
) -> tuple[LocalControlAdapter, RuntimeState, FakeServices, FakeHass]:
    states = FakeStates(
        {
            "number.fbp_min_soc": number(5),
            "number.fbp_max_soc": number(90, maximum=maximum),
            "select.fbp_mode": SimpleNamespace(state="Idle", attributes={}),
        }
    )
    services = FakeServices(states, apply_updates=apply_updates)
    hass = FakeHass(states, services)
    state = runtime()

    async def save() -> None:
        return None

    return LocalControlAdapter(hass, config, lambda: state, save), state, services, hass


def test_command_rejects_out_of_range_native_soc_limit_before_mode_change() -> None:
    control, state, services, _ = adapter(maximum=80)

    success, result = asyncio.run(
        control.async_command(Action.GRID, datetime.now(UTC), target_soc=90)
    )

    assert not success
    assert "outside native bounds" in result
    assert not any(domain == "select" for domain, _, _ in services.calls)
    assert not state.execution_enabled


def test_command_requires_soc_limit_readback_before_mode_change() -> None:
    control, state, services, hass = adapter(apply_updates=False)

    success, result = asyncio.run(
        control.async_command(Action.CHARGE, datetime.now(UTC), target_soc=90)
    )

    assert not success
    assert "SOC limit read-back" in result
    assert not any(domain == "select" for domain, _, _ in services.calls)
    assert hass.flushes > 0
    assert not state.execution_enabled


def test_command_confirms_limits_then_mode_after_event_queue_drains() -> None:
    control, state, services, hass = adapter()

    success, result = asyncio.run(
        control.async_command(Action.CHARGE, datetime.now(UTC), target_soc=90)
    )

    assert success
    assert result == "command confirmed by Operating Mode entity"
    assert state.last_action == Action.CHARGE.value
    assert any(domain == "select" for domain, _, _ in services.calls)
    assert hass.flushes >= 2


def test_direct_tcp_command_uses_allowlisted_client_and_updates_commanded_mode() -> None:
    class DirectClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def async_set_limits(self, minimum: int, maximum: int) -> None:
            self.calls.append(("limits", minimum, maximum))

        async def async_set_mode(
            self, mode: str, power: int, *, min_soc: int, max_soc: int
        ) -> None:
            self.calls.append(("mode", mode, power, min_soc, max_soc))

        async def async_set_self_consumption(self) -> None:
            self.calls.append(("self_consumption",))

    state = runtime()
    direct = DirectClient()
    modes: list[str] = []

    async def save() -> None:
        return None

    adapter = LocalControlAdapter(
        FakeHass(FakeStates({}), FakeServices(FakeStates({}))),
        config,
        lambda: state,
        save,
        lambda: direct,
        modes.append,
    )

    success, result = asyncio.run(
        adapter.async_command(Action.CHARGE, datetime.now(UTC), target_soc=90)
    )

    assert success
    assert "SOC limits read back" in result
    assert direct.calls == [("limits", 10, 90), ("mode", "Charge", 1200, 10, 90)]
    assert modes == ["Charge"]


def test_repeated_direct_command_does_not_consume_transition_budget() -> None:
    class DirectClient:
        async def async_set_limits(self, minimum: int, maximum: int) -> None:
            del minimum, maximum

        async def async_set_mode(
            self, mode: str, power: int, *, min_soc: int, max_soc: int
        ) -> None:
            del mode, power, min_soc, max_soc

        async def async_set_self_consumption(self) -> None:
            return None

    state = runtime()

    async def save() -> None:
        return None

    adapter = LocalControlAdapter(
        FakeHass(FakeStates({}), FakeServices(FakeStates({}))),
        config,
        lambda: state,
        save,
        DirectClient,
    )
    first = datetime(2026, 9, 21, 16, tzinfo=UTC)

    asyncio.run(adapter.async_command(Action.CHARGE, first, target_soc=90))
    asyncio.run(
        adapter.async_command(Action.CHARGE, first.replace(minute=1), target_soc=90)
    )

    assert state.transitions == [first.isoformat()]
    assert state.last_action_at == first.isoformat()


def test_command_disables_control_when_operating_mode_readback_is_stale() -> None:
    control, state, services, hass = adapter(apply_updates=False)
    hass.states.get("number.fbp_min_soc").state = "10"

    success, result = asyncio.run(
        control.async_command(Action.CHARGE, datetime.now(UTC), target_soc=90)
    )

    assert not success
    assert result == "mode read-back did not confirm Charge"
    assert any(domain == "select" for domain, _, _ in services.calls)
    assert not state.execution_enabled


def test_enabling_control_requires_commissioned_native_soc_controls() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(hass, Entry({"commissioned": True}))
        coordinator.async_request_refresh = _no_refresh

        with pytest.raises(ValueError, match="minimum and maximum SOC"):
            await coordinator.async_set_execution_enabled(True)

        assert not coordinator.runtime.execution_enabled

    asyncio.run(scenario())


def test_enabling_control_requires_available_native_soc_controls() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        data = config()
        coordinator = Fbp1200Coordinator(hass, Entry(data))
        coordinator.async_request_refresh = _no_refresh

        hass.states.async_set("number.fbp_min_soc", "10", {"min": 0, "max": 100})
        with pytest.raises(ValueError, match="maximum SOC control is unavailable"):
            await coordinator.async_set_execution_enabled(True)

        assert not coordinator.runtime.execution_enabled

    asyncio.run(scenario())


def test_enabling_control_requires_native_bounds_for_all_configured_soc_targets() -> (
    None
):
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        data = config()
        coordinator = Fbp1200Coordinator(hass, Entry(data))
        coordinator.async_request_refresh = _no_refresh
        hass.states.async_set("number.fbp_min_soc", "10", {"min": 0, "max": 100})
        hass.states.async_set("number.fbp_max_soc", "80", {"min": 0, "max": 80})

        with pytest.raises(ValueError, match="cannot accept configured"):
            await coordinator.async_set_execution_enabled(True)

        assert not coordinator.runtime.execution_enabled

    asyncio.run(scenario())


def test_enabling_control_accepts_commissioned_reachable_native_soc_controls() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(hass, Entry(config()))
        coordinator.store = StaticStore(RuntimeState())
        coordinator.async_request_refresh = _no_refresh
        hass.states.async_set("number.fbp_min_soc", "10", {"min": 0, "max": 100})
        hass.states.async_set("number.fbp_max_soc", "90", {"min": 0, "max": 100})

        await coordinator.async_set_execution_enabled(True)

        assert coordinator.runtime.execution_enabled

    asyncio.run(scenario())


def test_enabling_direct_control_recovers_legacy_rapid_transition_burst() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(
            hass, Entry({"host": "192.168.30.90", "commissioned": True})
        )
        coordinator.store = StaticStore(RuntimeState())
        coordinator.async_request_refresh = _no_refresh
        coordinator.async_soc_control_problems = _no_soc_control_problems
        coordinator._direct_load_problem = lambda: None
        first = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=4)
        coordinator.runtime.transitions = [
            (first + timedelta(minutes=minute)).isoformat() for minute in range(4)
        ]

        await coordinator.async_set_execution_enabled(True)

        assert coordinator.runtime.execution_enabled
        assert coordinator.runtime.transitions == [first.isoformat()]

    asyncio.run(scenario())


def test_restart_disables_persisted_control_without_native_soc_controls() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(hass, Entry({"commissioned": True}))
        persisted = RuntimeState(execution_enabled=True)
        store = StaticStore(persisted)
        coordinator.store = store

        await coordinator.async_initialize()

        assert not coordinator.runtime.execution_enabled
        assert store.saved

    asyncio.run(scenario())


def test_direct_restart_preserves_control_until_fresh_telemetry_validates_it() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(
            hass, Entry({"host": "192.168.30.90", "commissioned": True})
        )
        persisted = RuntimeState(execution_enabled=True)
        store = StaticStore(persisted)
        coordinator.store = store

        await coordinator.async_initialize()

        assert coordinator.runtime.execution_enabled
        assert store.saved  # Learner provenance is migrated before validation.
        assert coordinator._startup_control_gate_reason is not None

    asyncio.run(scenario())


def test_direct_load_history_is_reset_when_the_confirmed_source_changes() -> None:
    async def scenario() -> None:
        from homeassistant.core import HomeAssistant

        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(
            hass,
            Entry(
                {
                    "host": "192.168.30.90",
                    "direct_load_source": "off_grid_total",
                    "direct_load_confirmed": True,
                    "direct_load_confirmed_source": "off_grid_total",
                }
            ),
        )
        persisted = RuntimeState(load_learner_source="direct:smart_load")
        persisted.load_learner.recent_w = 442
        store = StaticStore(persisted)
        coordinator.store = store

        await coordinator.async_initialize()

        assert coordinator.runtime.load_learner_source == "direct:off_grid_total"
        assert not coordinator.runtime.load_learner.recent_w
        assert store.saved

    asyncio.run(scenario())


async def _no_refresh() -> None:
    """Prevent update scheduling; this test targets the control-enable seam only."""


async def _no_soc_control_problems() -> list[str]:
    """Treat native direct SOC controls as reachable for the recovery seam."""
    return []
