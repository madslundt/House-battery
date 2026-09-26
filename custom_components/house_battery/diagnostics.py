"""Redacted evidence export for diagnostics and offline analysis."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import Fbp1200Coordinator

_REDACT = {
    "host",
    "ip",
    "serial",
    "device_serial",
    "token",
    "password",
    "api_key",
    "ssid",
    "mac",
    "latitude",
    "longitude",
    "email",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the complete local evidence model with sensitive fields removed."""
    coordinator: Fbp1200Coordinator = entry.runtime_data
    return async_redact_data(
        {
            "integration": {"version": "1.5.1", "title": entry.title},
            "configuration": coordinator.config,
            "export": coordinator.export_data(),
        },
        _REDACT,
    )
