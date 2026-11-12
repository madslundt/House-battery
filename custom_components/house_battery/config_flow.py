"""Config and options flows for the battery optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_ACCOUNTING_LOAD_POWER,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_CHARGE_POWER_CONTROL,
    CONF_COMMISSIONED,
    CONF_DISCHARGE_POWER_CONTROL,
    CONF_DEVICE_TYPE,
    DEVICE_ENTITY_CONTROLS,
    DEVICE_ECOFLOW,
    DEVICE_FOSSIBOT,
    CONF_ECOFLOW_AC_PORT_SWITCH,
    CONF_ECOFLOW_CAPACITY_KWH,
    CONF_ECOFLOW_DISCHARGE_POWER_W,
    CONF_EFFICIENCY_GRID_POWER,
    CONF_FAULT,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT_POWER,
    CONF_GRID_ISOLATION_SWITCH,
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
from .ecoflow_capabilities import (
    ecoflow_charge_power_limit_w,
    ecoflow_soc_control_range,
)


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _unit_entity(domain: str, *units: str) -> selector.EntitySelector:
    """Offer only entities whose values use a unit consumed by the planner."""
    return selector.EntitySelector(selector.EntitySelectorConfig(
        domain=domain,
        filter=[{"domain": domain, "unit_of_measurement": unit} for unit in units],
    ))


def _validate_ecoflow_bindings(values: dict[str, Any]) -> None:
    """Reject shared entities whose independent EcoFlow controls conflict."""
    if values.get(CONF_MIN_SOC_CONTROL) == values.get(CONF_MAX_SOC_CONTROL):
        raise ValueError("EcoFlow charge and discharge limits need separate number entities")
    if values.get(CONF_GRID_ISOLATION_SWITCH) == values.get(CONF_ECOFLOW_AC_PORT_SWITCH):
        raise ValueError("EcoFlow grid isolation and AC Port need separate switch entities")


def _efficiency_meter() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(
        filter={"domain": "sensor", "unit_of_measurement": "W"}
    ))


def _required(key: str, defaults: dict[str, Any]) -> vol.Required:
    return (
        vol.Required(key, default=defaults[key])
        if key in defaults
        else vol.Required(key)
    )


def _optional(key: str, defaults: dict[str, Any]) -> vol.Optional:
    return (
        vol.Optional(key, description={"suggested_value": defaults[key]})
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
        _optional(CONF_EFFICIENCY_GRID_POWER, defaults): _efficiency_meter(),
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
        _optional(CONF_GRID_ISOLATION_SWITCH, defaults): _entity("switch"),
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
        _required(CONF_GRID_AVAILABLE, defaults): _entity(["sensor", "binary_sensor"]),
        _optional(CONF_GRID_IMPORT_POWER, defaults): _entity("sensor"),
        _optional(CONF_EFFICIENCY_GRID_POWER, defaults): _efficiency_meter(),
        _optional(CONF_ACCOUNTING_LOAD_POWER, defaults): _entity("sensor"),
        _required(CONF_PRICE_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "binary_sensor"], multiple=True
            )
        ),
        _optional(CONF_PRICE_FORECAST_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True)
        ),
        _optional(CONF_GRID_ISOLATION_SWITCH, defaults): _entity("switch"),
    }
    if options:
        fields[
            vol.Required(
                CONF_COMMISSIONED, default=defaults.get(CONF_COMMISSIONED, False)
            )
        ] = bool
    return vol.Schema(fields)


def _ecoflow_schema(defaults: dict[str, Any], *, options: bool = False) -> vol.Schema:
    """Bind the EF BLE telemetry and controls used by EcoFlow power stations."""
    defaults = _with_forecast_defaults(defaults)
    fields: dict[Any, Any] = {
        _required(CONF_SOC, defaults): _unit_entity("sensor", "%"),
        _required(CONF_LOAD_POWER, defaults): _unit_entity("sensor", "W"),
        _required(CONF_GRID_AVAILABLE, defaults): _entity(["sensor", "binary_sensor"]),
        _required(CONF_PRICE_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor", "binary_sensor"], multiple=True)
        ),
        _optional(CONF_PRICE_FORECAST_ENTITIES, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True)
        ),
        _optional(CONF_GRID_IMPORT_POWER, defaults): _unit_entity("sensor", "W"),
        _optional(CONF_EFFICIENCY_GRID_POWER, defaults): _efficiency_meter(),
        _optional(CONF_ACCOUNTING_LOAD_POWER, defaults): _unit_entity("sensor", "W"),
        _required(CONF_GRID_ISOLATION_SWITCH, defaults): _entity("switch"),
        _required(CONF_BATTERY_CHARGE_POWER, defaults): _unit_entity("sensor", "W"),
        _required(CONF_BATTERY_DISCHARGE_POWER, defaults): _unit_entity("sensor", "W"),
        _optional(CONF_CHARGE_POWER_CONTROL, defaults): _unit_entity("number", "W", "kW"),
        _required(CONF_MIN_SOC_CONTROL, defaults): _unit_entity("number", "%"),
        _required(CONF_MAX_SOC_CONTROL, defaults): _unit_entity("number", "%"),
        _required(CONF_ECOFLOW_AC_PORT_SWITCH, defaults): _entity("switch"),
    }
    if options:
        fields[vol.Required(
            CONF_COMMISSIONED, default=defaults.get(CONF_COMMISSIONED, False)
        )] = bool
    return vol.Schema(fields)


def _ecoflow_ratings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Ask only for physical ratings not exposed as writable device limits."""
    return vol.Schema({
        vol.Required(CONF_ECOFLOW_CAPACITY_KWH): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.1, max=100.0, step=0.001, unit_of_measurement="kWh",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_ECOFLOW_DISCHARGE_POWER_W): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=100, max=12000, step=50, unit_of_measurement="W",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, NAME)): str,
    })


def _legacy_device_type_schema() -> vol.Schema:
    """Let pre-device-type entity entries opt into the correct adapter."""
    return vol.Schema({
        vol.Required(CONF_DEVICE_TYPE, default=DEVICE_ENTITY_CONTROLS): selector.SelectSelector(
            selector.SelectSelectorConfig(options=[
                {"value": DEVICE_ENTITY_CONTROLS, "label": "Keep existing entity controls"},
                {"value": DEVICE_ECOFLOW, "label": "EcoFlow (EF BLE entities)"},
            ], mode=selector.SelectSelectorMode.DROPDOWN)
        )
    })


class Fbp1200ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an optimizer entry with a local battery connection."""

    VERSION = 1
    MINOR_VERSION = 2

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
            if user_input[CONF_DEVICE_TYPE] == DEVICE_ECOFLOW:
                self._connection = {CONF_DEVICE_TYPE: DEVICE_ECOFLOW}
                return await self.async_step_ecoflow()
            self._connection = {CONF_DEVICE_TYPE: DEVICE_FOSSIBOT}
            return await self.async_step_fossibot()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_TYPE, default=DEVICE_FOSSIBOT): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        {"value": DEVICE_FOSSIBOT, "label": "FOSSiBOT (local TCP)"},
                        {"value": DEVICE_ECOFLOW, "label": "EcoFlow (EF BLE entities)"},
                    ], mode=selector.SelectSelectorMode.DROPDOWN)
                )
            }),
        )

    async def async_step_fossibot(
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
                    step_id="fossibot",
                    data_schema=self._connection_schema(user_input),
                    errors=errors,
                )
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            self._connection = {
                CONF_DEVICE_TYPE: DEVICE_FOSSIBOT,
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_NAME: user_input[CONF_NAME].strip() or NAME,
            }
            return await self.async_step_household()
        return self.async_show_form(
            step_id="fossibot", data_schema=self._connection_schema({})
        )

    async def async_step_ecoflow(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            try:
                _validate_ecoflow_bindings(user_input)
                charge_entity = user_input.get(CONF_CHARGE_POWER_CONTROL)
                if charge_entity:
                    charge_state = self.hass.states.get(charge_entity)
                    if charge_state is None:
                        raise ValueError("AC Charging Speed is unavailable")
                    ecoflow_charge_power_limit_w(charge_state)
                for key in (CONF_MIN_SOC_CONTROL, CONF_MAX_SOC_CONTROL):
                    state = self.hass.states.get(user_input[key])
                    if state is None or str(
                        state.attributes.get("unit_of_measurement", "")
                    ).strip() != "%":
                        raise ValueError(f"{key} must be an available percent control")
                    ecoflow_soc_control_range(state)
            except ValueError:
                return self.async_show_form(
                    step_id="ecoflow",
                    data_schema=_ecoflow_schema(user_input),
                    errors={"base": "invalid_device_control"},
                )
            self._connection = dict(user_input)
            return await self.async_step_ecoflow_ratings()
        return self.async_show_form(
            step_id="ecoflow",
            data_schema=_ecoflow_schema({}),
        )

    async def async_step_ecoflow_ratings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            soc_entity = self._connection[CONF_SOC]
            await self.async_set_unique_id(f"ecoflow:{soc_entity}")
            self._abort_if_unique_id_configured()
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
        return self.async_show_form(
            step_id="ecoflow_ratings",
            data_schema=_ecoflow_ratings_schema({}),
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
        self._selected_device_type: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        defaults = {**self._entry.data, **self._entry.options}
        device_type = defaults.get(CONF_DEVICE_TYPE)
        if user_input is not None:
            if device_type == DEVICE_ECOFLOW:
                try:
                    _validate_ecoflow_bindings(user_input)
                    charge_entity = user_input.get(CONF_CHARGE_POWER_CONTROL)
                    if charge_entity:
                        charge_state = self.hass.states.get(charge_entity)
                        if charge_state is None:
                            raise ValueError("AC Charging Speed is unavailable")
                        ecoflow_charge_power_limit_w(charge_state)
                    for key in (CONF_MIN_SOC_CONTROL, CONF_MAX_SOC_CONTROL):
                        state = self.hass.states.get(user_input[key])
                        if state is None or str(
                            state.attributes.get("unit_of_measurement", "")
                        ).strip() != "%":
                            raise ValueError(f"{key} must be an available percent control")
                        ecoflow_soc_control_range(state)
                except (KeyError, ValueError):
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_ecoflow_schema(defaults, options=True),
                        errors={"base": "invalid_device_control"},
                    )
            optional_keys = (
                (CONF_GRID_IMPORT_POWER, CONF_ACCOUNTING_LOAD_POWER)
                if self._entry.data.get(CONF_HOST)
                else (
                    CONF_FAULT, CONF_ONLINE,
                    CONF_CHARGE_POWER_CONTROL, CONF_DISCHARGE_POWER_CONTROL,
                )
            )
            # Explicit cleared values override the original setup bindings
            # when the coordinator merges entry data with these options.
            data = {
                **dict.fromkeys((
                    *optional_keys,
                    CONF_GRID_ISOLATION_SWITCH,
                    CONF_EFFICIENCY_GRID_POWER,
                )),
                CONF_PRICE_FORECAST_ENTITIES: [],
                CONF_PRICE_FORECAST_ENTITY: None,
                **user_input,
                **(
                    {CONF_DEVICE_TYPE: DEVICE_ENTITY_CONTROLS}
                    if self._selected_device_type == DEVICE_ENTITY_CONTROLS
                    else {}
                ),
            }
            return self.async_create_entry(title="", data=data)
        if self._entry.data.get(CONF_HOST):
            return self.async_show_form(
                step_id="init", data_schema=_direct_schema(defaults, options=True)
            )
        if device_type == DEVICE_ECOFLOW:
            return self.async_show_form(
                step_id="init", data_schema=_ecoflow_schema(defaults, options=True)
            )
        if not device_type:
            return self.async_show_form(
                step_id="select_device_type",
                data_schema=_legacy_device_type_schema(),
            )
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults, options=True)
        )

    async def async_step_select_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Offer an EcoFlow migration path for legacy entity-based entries."""
        defaults = {**self._entry.data, **self._entry.options}
        if user_input is None:
            return self.async_show_form(
                step_id="select_device_type",
                data_schema=_legacy_device_type_schema(),
            )
        if user_input[CONF_DEVICE_TYPE] == DEVICE_ECOFLOW:
            return self.async_show_form(
                step_id="ecoflow_legacy",
                data_schema=_ecoflow_schema(defaults, options=True),
            )
        self._selected_device_type = DEVICE_ENTITY_CONTROLS
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults, options=True)
        )

    async def async_step_ecoflow_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Migrate a legacy entity entry to EF BLE control bindings."""
        defaults = {**self._entry.data, **self._entry.options}
        if user_input is None:
            return self.async_show_form(
                step_id="ecoflow_legacy",
                data_schema=_ecoflow_schema(defaults, options=True),
            )
        try:
            _validate_ecoflow_bindings(user_input)
            charge_entity = user_input.get(CONF_CHARGE_POWER_CONTROL)
            if charge_entity:
                charge_state = self.hass.states.get(charge_entity)
                if charge_state is None:
                    raise ValueError("AC Charging Speed is unavailable")
                ecoflow_charge_power_limit_w(charge_state)
            for key in (CONF_MIN_SOC_CONTROL, CONF_MAX_SOC_CONTROL):
                state = self.hass.states.get(user_input[key])
                if state is None or str(
                    state.attributes.get("unit_of_measurement", "")
                ).strip() != "%":
                    raise ValueError(f"{key} must be an available percent control")
                ecoflow_soc_control_range(state)
        except (KeyError, ValueError):
            return self.async_show_form(
                step_id="ecoflow_legacy",
                data_schema=_ecoflow_schema(defaults, options=True),
                errors={"base": "invalid_device_control"},
            )
        data = {
            **dict.fromkeys((CONF_OPERATING_MODE, CONF_DISCHARGE_POWER_CONTROL)),
            CONF_PRICE_FORECAST_ENTITIES: [],
            CONF_PRICE_FORECAST_ENTITY: None,
            **user_input,
            CONF_DEVICE_TYPE: DEVICE_ECOFLOW,
        }
        return self.async_create_entry(title="", data=data)
