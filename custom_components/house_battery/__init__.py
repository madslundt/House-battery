"""FOSSiBOT FBP1200 Optimizer integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import CONF_COMMISSIONED, DOMAIN, PLATFORMS
from .coordinator import Fbp1200Coordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEDULE_LOAD = "schedule_load"
SERVICE_CLEAR_SCHEDULED_LOADS = "clear_scheduled_loads"
SERVICE_EXPORT_DATA = "export_data"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register integration-level services."""

    def coordinator_for(call: ServiceCall) -> Fbp1200Coordinator:
        entry_id = call.data.get("config_entry_id")
        coordinators: dict[str, Fbp1200Coordinator] = hass.data.get(DOMAIN, {})
        if entry_id:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise vol.Invalid(f"Unknown FBP1200 config entry: {entry_id}")
            return coordinator
        if len(coordinators) != 1:
            raise vol.Invalid("config_entry_id is required when multiple entries exist")
        return next(iter(coordinators.values()))

    async def schedule_load(call: ServiceCall) -> None:
        coordinator = coordinator_for(call)
        start = _parse_datetime(call.data["start"])
        end = _parse_datetime(call.data["end"])
        await coordinator.async_add_scheduled_load(
            start,
            end,
            float(call.data["additional_w"]),
            call.data.get("label", "scheduled load"),
        )

    async def clear_scheduled_loads(call: ServiceCall) -> None:
        coordinator = coordinator_for(call)
        coordinator.runtime.scheduled_loads.clear()
        await coordinator.store.save(coordinator.runtime)
        await coordinator.async_request_refresh()

    async def export_data(call: ServiceCall) -> dict[str, Any]:
        return coordinator_for(call).export_data()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SCHEDULE_LOAD,
        schedule_load,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("start"): cv.string,
                vol.Required("end"): cv.string,
                vol.Required("additional_w"): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=20000)
                ),
                vol.Optional("label", default="scheduled load"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_SCHEDULED_LOADS,
        clear_scheduled_loads,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_DATA,
        export_data,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    return True


def _parse_datetime(value: str) -> datetime:
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise vol.Invalid("start and end must be ISO 8601 timestamps with an offset")
    return parsed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one FBP1200 optimizer."""
    coordinator = Fbp1200Coordinator(hass, entry)
    await coordinator.async_initialize()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and persist the latest local evidence."""
    coordinator: Fbp1200Coordinator = entry.runtime_data
    await coordinator.store.save(coordinator.runtime)
    if coordinator.local_client is not None:
        await coordinator.local_client.async_close()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator: Fbp1200Coordinator = entry.runtime_data
    commissioned = bool({**entry.data, **entry.options}.get(CONF_COMMISSIONED, False))
    if coordinator.runtime.execution_enabled and not commissioned:
        await coordinator.async_force_safe()
    await hass.config_entries.async_reload(entry.entry_id)
