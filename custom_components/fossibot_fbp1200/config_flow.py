"""Config and options flows for the FBP1200 optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_CHARGE_POWER_CONTROL,
    CONF_COMMISSIONED,
    CONF_DISCHARGE_POWER_CONTROL,
    CONF_FAULT,
    CONF_GRID_AVAILABLE,
    CONF_GRID_EXPORT_POWER,
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    CONF_ONLINE,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_PV_POWER,
    CONF_SOC,
    DOMAIN,
    NAME,
)


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _required(key: str, defaults: dict[str, Any]) -> vol.Required:
    return (
        vol.Required(key, default=defaults[key])
        if key in defaults
        else vol.Required(key)
    )


def _optional(key: str, defaults: dict[str, Any]) -> vol.Optional:
    return (
        vol.Optional(key, default=defaults[key])
        if key in defaults
        else vol.Optional(key)
    )


def _schema(defaults: dict[str, Any], *, options: bool = False) -> vol.Schema:
    fields: dict[Any, Any] = {
        _required(CONF_SOC, defaults): _entity("sensor"),
        _required(CONF_LOAD_POWER, defaults): _entity("sensor"),
        _required(CONF_GRID_IMPORT_POWER, defaults): _entity("sensor"),
        _required(CONF_GRID_AVAILABLE, defaults): _entity(["sensor", "binary_sensor"]),
        _required(CONF_OPERATING_MODE, defaults): _entity("select"),
        _required(CONF_PRICE_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "binary_sensor"], multiple=True
            )
        ),
        _optional(CONF_GRID_EXPORT_POWER, defaults): _entity("sensor"),
        _required(CONF_BATTERY_CHARGE_POWER, defaults): _entity("sensor"),
        _required(CONF_BATTERY_DISCHARGE_POWER, defaults): _entity("sensor"),
        _optional(CONF_PV_POWER, defaults): _entity("sensor"),
        _optional(CONF_FAULT, defaults): _entity(["sensor", "binary_sensor"]),
        _optional(CONF_ONLINE, defaults): _entity(["sensor", "binary_sensor"]),
        _optional(CONF_CHARGE_POWER_CONTROL, defaults): _entity("number"),
        _optional(CONF_DISCHARGE_POWER_CONTROL, defaults): _entity("number"),
        _optional(CONF_MIN_SOC_CONTROL, defaults): _entity("number"),
        _optional(CONF_MAX_SOC_CONTROL, defaults): _entity("number"),
    }
    if options:
        fields[
            vol.Required(
                CONF_COMMISSIONED, default=defaults.get(CONF_COMMISSIONED, False)
            )
        ] = bool
    else:
        fields[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, NAME))] = str
    return vol.Schema(fields)


class Fbp1200ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Bind an optimizer entry to locally managed FBP1200 entities."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> Fbp1200OptionsFlow:
        return Fbp1200OptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            mode_entity = user_input[CONF_OPERATING_MODE]
            await self.async_set_unique_id(mode_entity)
            self._abort_if_unique_id_configured()
            title = user_input.pop(CONF_NAME).strip() or NAME
            user_input = {
                key: value
                for key, value in user_input.items()
                if value not in (None, "")
            }
            user_input[CONF_COMMISSIONED] = False
            return self.async_create_entry(title=title, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))


class Fbp1200OptionsFlow(config_entries.OptionsFlow):
    """Allow safe rebinding and explicit commissioning."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        defaults = {**self._entry.data, **self._entry.options}
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults, options=True)
        )
