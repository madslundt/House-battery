"""Config and options flows for the battery optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
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
    CONF_GRID_IMPORT_POWER,
    CONF_LOAD_POWER,
    CONF_MAX_SOC_CONTROL,
    CONF_MIN_SOC_CONTROL,
    CONF_ONLINE,
    CONF_OPERATING_MODE,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITIES,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_SOC,
    DEFAULT_PORT,
    DOMAIN,
    NAME,
)
from .local_tcp import FbpLocalTcpClient, LocalProtocolError


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


def _with_forecast_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    """Expose legacy single-source settings in the new multi-source selector."""
    result = dict(defaults)
    if CONF_PRICE_FORECAST_ENTITIES not in result:
        legacy = result.get(CONF_PRICE_FORECAST_ENTITY)
        if legacy:
            result[CONF_PRICE_FORECAST_ENTITIES] = [legacy]
    return result


def _schema(defaults: dict[str, Any], *, options: bool = False) -> vol.Schema:
    defaults = _with_forecast_defaults(defaults)
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
        _optional(CONF_PRICE_FORECAST_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True)
        ),
        _required(CONF_BATTERY_CHARGE_POWER, defaults): _entity("sensor"),
        _required(CONF_BATTERY_DISCHARGE_POWER, defaults): _entity("sensor"),
        _optional(CONF_FAULT, defaults): _entity(["sensor", "binary_sensor"]),
        _optional(CONF_ONLINE, defaults): _entity(["sensor", "binary_sensor"]),
        _optional(CONF_CHARGE_POWER_CONTROL, defaults): _entity("number"),
        _optional(CONF_DISCHARGE_POWER_CONTROL, defaults): _entity("number"),
    }
    if options:
        # The native SOC limits are mandatory before a user can complete the
        # commissioning step. Runtime availability is checked again on enable.
        fields[_required(CONF_MIN_SOC_CONTROL, defaults)] = _entity("number")
        fields[_required(CONF_MAX_SOC_CONTROL, defaults)] = _entity("number")
        fields[
            vol.Required(
                CONF_COMMISSIONED, default=defaults.get(CONF_COMMISSIONED, False)
            )
        ] = bool
    else:
        fields[_optional(CONF_MIN_SOC_CONTROL, defaults)] = _entity("number")
        fields[_optional(CONF_MAX_SOC_CONTROL, defaults)] = _entity("number")
        fields[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, NAME))] = str
    return vol.Schema(fields)


def _direct_schema(defaults: dict[str, Any], *, options: bool = False) -> vol.Schema:
    """Fields still supplied by Home Assistant, not by the battery TCP API."""
    defaults = _with_forecast_defaults(defaults)
    fields: dict[Any, Any] = {
        # Signed grid flow is the TCP meter (MeterTotalActivePower), read from
        # the battery directly, so no HA grid-import entity is required. An
        # optional HA sensor may be supplied only as an independent cross-check.
        _optional(CONF_GRID_IMPORT_POWER, defaults): _entity("sensor"),
        _required(CONF_GRID_AVAILABLE, defaults): _entity(["sensor", "binary_sensor"]),
        _required(CONF_PRICE_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "binary_sensor"], multiple=True
            )
        ),
        _optional(CONF_PRICE_FORECAST_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True)
        ),
    }
    if options:
        fields[
            vol.Required(
                CONF_COMMISSIONED, default=defaults.get(CONF_COMMISSIONED, False)
            )
        ] = bool
    return vol.Schema(fields)


class Fbp1200ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an optimizer entry with a local battery connection."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> Fbp1200OptionsFlow:
        return Fbp1200OptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            errors: dict[str, str] = {}
            try:
                client = FbpLocalTcpClient(host, port)
                await client.async_snapshot()
                await client.async_close()
            except (LocalProtocolError, ValueError):
                errors["base"] = "cannot_connect"
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._connection_schema(user_input),
                    errors=errors,
                )
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            self._connection = {
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_NAME: user_input[CONF_NAME].strip() or NAME,
            }
            return await self.async_step_household()
        return self.async_show_form(
            step_id="user", data_schema=self._connection_schema({})
        )

    def _connection_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, NAME)): str,
            }
        )

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            data = {
                **self._connection,
                **{
                    key: value
                    for key, value in user_input.items()
                    if value not in (None, "")
                },
                CONF_COMMISSIONED: False,
            }
            return self.async_create_entry(title=data[CONF_NAME], data=data)
        return self.async_show_form(step_id="household", data_schema=_direct_schema({}))


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
        if self._entry.data.get(CONF_HOST):
            return self.async_show_form(
                step_id="init", data_schema=_direct_schema(defaults, options=True)
            )
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults, options=True)
        )
