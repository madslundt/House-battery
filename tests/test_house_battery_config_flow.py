"""Configuration changes replace optional bindings without reviving originals."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.config_flow import (
    Fbp1200ConfigFlow,
    Fbp1200OptionsFlow,
    _direct_schema,
    _schema,
    _ecoflow_ratings_schema,
    _validate_ecoflow_bindings,
)
from house_battery.const import (
    CONF_ACCOUNTING_LOAD_POWER,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_CHARGE_POWER_CONTROL,
    CONF_COMMISSIONED,
    CONF_DEVICE_TYPE,
    DEVICE_ECOFLOW,
    CONF_DISCHARGE_POWER_CONTROL,
    CONF_ECOFLOW_AC_PORT_SWITCH,
    CONF_ECOFLOW_CAPACITY_KWH,
    CONF_ECOFLOW_CHARGE_POWER_W,
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
)


def test_untyped_entity_based_entry_can_be_switched_to_ecoflow() -> None:
    async def scenario() -> None:
        original = {
            CONF_SOC: "sensor.battery_level",
            CONF_LOAD_POWER: "sensor.load",
            CONF_GRID_AVAILABLE: "binary_sensor.grid",
            CONF_PRICE_ENTITIES: ["sensor.price"],
            CONF_OPERATING_MODE: None,
        }
        flow = Fbp1200OptionsFlow(SimpleNamespace(data=original, options={}))

        device_form = await flow.async_step_init()

        assert device_form["step_id"] == "select_device_type"
        selected = device_form["data_schema"]({CONF_DEVICE_TYPE: DEVICE_ECOFLOW})
        ecoflow_form = await flow.async_step_select_device_type(selected)

        assert ecoflow_form["step_id"] == "ecoflow_legacy"
        schema = ecoflow_form["data_schema"].schema
        assert CONF_OPERATING_MODE not in schema
        assert any(key == CONF_ECOFLOW_AC_PORT_SWITCH for key in schema)

    asyncio.run(scenario())


def test_ecoflow_migration_persists_type_and_requires_explicit_commissioning() -> None:
    async def scenario() -> None:
        original = {
            CONF_SOC: "sensor.battery_level",
            CONF_LOAD_POWER: "sensor.load",
            CONF_GRID_AVAILABLE: "binary_sensor.grid",
            CONF_PRICE_ENTITIES: ["sensor.price"],
        }
        flow = Fbp1200OptionsFlow(SimpleNamespace(data=original, options={}))
        flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: {
            "number.min": SimpleNamespace(
                state="5", attributes={"unit_of_measurement": "%", "min": 0,
                                         "max": 100, "step": 1}
            ),
            "number.max": SimpleNamespace(
                state="80", attributes={"unit_of_measurement": "%", "min": 0,
                                         "max": 100, "step": 1}
            ),
            "number.charge": SimpleNamespace(
                state="640", attributes={"unit_of_measurement": "W", "min": 100,
                                          "max": 640, "step": 10}
            ),
        }.get(entity_id)))

        type_form = await flow.async_step_init()
        ecoflow_form = await flow.async_step_select_device_type(
            type_form["data_schema"]({CONF_DEVICE_TYPE: DEVICE_ECOFLOW})
        )
        values = {
            CONF_SOC: "sensor.battery_level",
            CONF_LOAD_POWER: "sensor.load",
            CONF_GRID_AVAILABLE: "binary_sensor.grid",
            CONF_PRICE_ENTITIES: ["sensor.price"],
            CONF_BATTERY_CHARGE_POWER: "sensor.ac_input",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.ac_output",
            CONF_CHARGE_POWER_CONTROL: "number.charge",
            CONF_MIN_SOC_CONTROL: "number.min",
            CONF_MAX_SOC_CONTROL: "number.max",
            CONF_ECOFLOW_AC_PORT_SWITCH: "switch.ac_port",
            CONF_GRID_ISOLATION_SWITCH: "switch.grid_isolation",
            CONF_COMMISSIONED: False,
        }

        result = await flow.async_step_ecoflow_legacy(
            ecoflow_form["data_schema"](values)
        )

        assert result["type"] == "create_entry"
        assert result["data"][CONF_DEVICE_TYPE] == DEVICE_ECOFLOW
        assert result["data"][CONF_COMMISSIONED] is False
        assert result["data"][CONF_OPERATING_MODE] is None
        assert result["data"][CONF_DISCHARGE_POWER_CONTROL] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("direct_local", [True, False])
def test_options_can_remove_optional_bindings_selected_during_setup(direct_local: bool) -> None:
    async def scenario() -> None:
        submitted = {
            CONF_GRID_AVAILABLE: "binary_sensor.grid",
            CONF_PRICE_ENTITIES: ["sensor.price", "binary_sensor.tomorrow"],
            CONF_COMMISSIONED: True,
        }
        optional = {
            CONF_GRID_ISOLATION_SWITCH: "switch.old_plug",
            CONF_PRICE_FORECAST_ENTITIES: ["sensor.old_forecast"],
            CONF_EFFICIENCY_GRID_POWER: "sensor.old_efficiency_phase",
        }
        if direct_local:
            optional.update({
                CONF_GRID_IMPORT_POWER: "sensor.old_grid",
                CONF_ACCOUNTING_LOAD_POWER: "sensor.old_house_load",
            })
        else:
            submitted.update({
                CONF_SOC: "sensor.soc", CONF_LOAD_POWER: "sensor.load",
                CONF_GRID_IMPORT_POWER: "sensor.grid", CONF_OPERATING_MODE: "select.mode",
                CONF_BATTERY_CHARGE_POWER: "sensor.charge",
                CONF_BATTERY_DISCHARGE_POWER: "sensor.discharge",
                CONF_MIN_SOC_CONTROL: "number.minimum", CONF_MAX_SOC_CONTROL: "number.maximum",
            })
            optional.update({
                CONF_FAULT: "binary_sensor.old_fault", CONF_ONLINE: "binary_sensor.old_online",
                CONF_CHARGE_POWER_CONTROL: "number.old_charge",
                CONF_DISCHARGE_POWER_CONTROL: "number.old_discharge",
            })
        original = {**submitted, **optional, CONF_PRICE_FORECAST_ENTITY: "sensor.legacy_forecast"}
        if direct_local:
            original["host"] = "192.0.2.1"
        flow = Fbp1200OptionsFlow(SimpleNamespace(data=original, options={}))
        form = await flow.async_step_init()
        if not direct_local:
            legacy_choice = form["data_schema"]({})
            form = await flow.async_step_select_device_type(legacy_choice)
        validated = form["data_schema"](submitted)
        result = await flow.async_step_init(validated)
        merged = {**original, **result["data"]}

        assert all(not merged.get(key) for key in optional)
        assert not merged.get(CONF_PRICE_FORECAST_ENTITY)
        assert merged[CONF_PRICE_ENTITIES] == ["sensor.price", "binary_sensor.tomorrow"]
        assert merged[CONF_COMMISSIONED]

    asyncio.run(scenario())


@pytest.mark.parametrize("direct_local", [True, False])
def test_efficiency_source_is_optional_and_separate_from_safety_meter(
    direct_local: bool,
) -> None:
    """Selecting a phase for learning must not replace the whole-site safety meter."""
    submitted = {
        CONF_GRID_AVAILABLE: "binary_sensor.grid",
        CONF_PRICE_ENTITIES: ["sensor.price"],
        CONF_GRID_IMPORT_POWER: "sensor.whole_site_grid",
    }
    if not direct_local:
        submitted.update({
            "name": "Battery",
            CONF_SOC: "sensor.soc",
            CONF_LOAD_POWER: "sensor.load",
            CONF_OPERATING_MODE: "select.mode",
            CONF_BATTERY_CHARGE_POWER: "sensor.charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.discharge",
        })
    schema = (_direct_schema if direct_local else _schema)({})

    assert CONF_EFFICIENCY_GRID_POWER not in schema(submitted)
    validated = schema({
        **submitted,
        CONF_EFFICIENCY_GRID_POWER: "sensor.watts_live_phase_l1",
    })
    assert validated[CONF_GRID_IMPORT_POWER] == "sensor.whole_site_grid"
    assert validated[CONF_EFFICIENCY_GRID_POWER] == "sensor.watts_live_phase_l1"
    key = next(key for key in schema.schema if key == CONF_EFFICIENCY_GRID_POWER)
    assert schema.schema[key].config["filter"] == [
        {"domain": ["sensor"], "unit_of_measurement": ["W"]}
    ]


@pytest.mark.parametrize("direct_local", [True, False])
def test_options_replace_efficiency_source_without_rebinding_grid_safety(
    direct_local: bool,
) -> None:
    async def scenario() -> None:
        original = {
            CONF_GRID_AVAILABLE: "binary_sensor.grid",
            CONF_PRICE_ENTITIES: ["sensor.price"],
            CONF_GRID_IMPORT_POWER: "sensor.whole_site_grid",
            CONF_EFFICIENCY_GRID_POWER: "sensor.old_phase",
        }
        submitted = {**original, CONF_EFFICIENCY_GRID_POWER: "sensor.new_phase"}
        if direct_local:
            original["host"] = "192.0.2.1"
        else:
            submitted.update({
                CONF_SOC: "sensor.soc", CONF_LOAD_POWER: "sensor.load",
                CONF_OPERATING_MODE: "select.mode",
                CONF_BATTERY_CHARGE_POWER: "sensor.charge",
                CONF_BATTERY_DISCHARGE_POWER: "sensor.discharge",
                CONF_MIN_SOC_CONTROL: "number.minimum",
                CONF_MAX_SOC_CONTROL: "number.maximum",
            })
        flow = Fbp1200OptionsFlow(SimpleNamespace(data=original, options={}))
        form = await flow.async_step_init()
        if not direct_local:
            legacy_choice = form["data_schema"]({})
            form = await flow.async_step_select_device_type(legacy_choice)
        result = await flow.async_step_init(form["data_schema"](submitted))
        merged = {**original, **result["data"]}

        assert merged[CONF_EFFICIENCY_GRID_POWER] == "sensor.new_phase"
        assert merged[CONF_GRID_IMPORT_POWER] == "sensor.whole_site_grid"

    asyncio.run(scenario())


@pytest.mark.parametrize("efficiency_source", [None, "sensor.watts_live_phase_l1"])
def test_direct_setup_stores_only_explicit_efficiency_source(
    efficiency_source: str | None,
) -> None:
    async def scenario() -> None:
        flow = Fbp1200ConfigFlow()
        flow._connection = {"host": "192.0.2.1", "port": 8080, "name": "Battery"}
        submitted = {
            CONF_GRID_AVAILABLE: "binary_sensor.grid",
            CONF_PRICE_ENTITIES: ["sensor.price"],
        }
        if efficiency_source:
            submitted[CONF_EFFICIENCY_GRID_POWER] = efficiency_source
        form = await flow.async_step_household()
        result = await flow.async_step_household(form["data_schema"](submitted))

        assert result["data"].get(CONF_EFFICIENCY_GRID_POWER) == efficiency_source
        assert not result["data"][CONF_COMMISSIONED]

    asyncio.run(scenario())


def test_ecoflow_setup_limits_entity_choices_and_uses_device_ratings() -> None:
    from house_battery.config_flow import _ecoflow_schema

    bindings = {
        CONF_SOC: "sensor.river_battery_level",
        CONF_LOAD_POWER: "sensor.river_ac_output_power",
        CONF_GRID_AVAILABLE: "binary_sensor.grid_available",
        CONF_PRICE_ENTITIES: ["sensor.price"],
        CONF_BATTERY_CHARGE_POWER: "sensor.river_input_power",
        CONF_BATTERY_DISCHARGE_POWER: "sensor.river_output_power",
        CONF_CHARGE_POWER_CONTROL: "number.river_ac_charging_speed",
        CONF_MIN_SOC_CONTROL: "number.river_battery_discharge_limit",
        CONF_MAX_SOC_CONTROL: "number.river_battery_charge_limit",
        CONF_ECOFLOW_AC_PORT_SWITCH: "switch.river_ac_port",
        CONF_GRID_ISOLATION_SWITCH: "switch.grid_isolation",
    }
    schema = _ecoflow_schema({})
    assert schema(bindings) == bindings
    assert CONF_ECOFLOW_CHARGE_POWER_W not in schema.schema
    assert schema({key: value for key, value in bindings.items() if key != CONF_CHARGE_POWER_CONTROL})
    charge_key = next(key for key in schema.schema if key == CONF_CHARGE_POWER_CONTROL)
    assert schema.schema[charge_key].config["filter"] == [
        {"domain": ["number"], "unit_of_measurement": ["W"]},
        {"domain": ["number"], "unit_of_measurement": ["kW"]},
    ]

    ratings = _ecoflow_ratings_schema({})
    rating_values = {
        "name": "River 3 Plus",
        CONF_ECOFLOW_CAPACITY_KWH: 0.286,
        CONF_ECOFLOW_DISCHARGE_POWER_W: 600,
    }
    assert ratings(rating_values) == rating_values
    capacity_key = next(key for key in ratings.schema if key == CONF_ECOFLOW_CAPACITY_KWH)
    assert ratings.schema[capacity_key].config["unit_of_measurement"] == "kWh"
    minimum_key = next(key for key in schema.schema if key == CONF_MIN_SOC_CONTROL)
    assert schema.schema[minimum_key].config["filter"] == [
        {"domain": ["number"], "unit_of_measurement": ["%"]},
    ]
    incomplete = dict(bindings)
    incomplete.pop(CONF_GRID_ISOLATION_SWITCH)
    with pytest.raises(Exception, match="required key not provided"):
        schema(incomplete)


@pytest.mark.parametrize("duplicate", [
    (CONF_MIN_SOC_CONTROL, CONF_MAX_SOC_CONTROL),
    (CONF_GRID_ISOLATION_SWITCH, CONF_ECOFLOW_AC_PORT_SWITCH),
])
def test_ecoflow_controls_must_use_distinct_entities(duplicate) -> None:
    bindings = {
        CONF_MIN_SOC_CONTROL: "number.minimum",
        CONF_MAX_SOC_CONTROL: "number.maximum",
        CONF_GRID_ISOLATION_SWITCH: "switch.grid",
        CONF_ECOFLOW_AC_PORT_SWITCH: "switch.ac_port",
    }
    left, right = duplicate
    bindings[right] = bindings[left]

    with pytest.raises(ValueError, match="separate"):
        _validate_ecoflow_bindings(bindings)


def test_ecoflow_device_bounds_are_normalized_by_unit() -> None:
    from house_battery.ecoflow_capabilities import (
        ecoflow_charge_power_limit_w,
        ecoflow_soc_control_range,
    )

    def state(unit: str, minimum: float, maximum: float, step: float):
        return SimpleNamespace(state=str(minimum), attributes={
            "unit_of_measurement": unit,
            "min": minimum,
            "max": maximum,
            "step": step,
        })

    assert ecoflow_charge_power_limit_w(state("W", 100, 640, 10)) == 640
    assert ecoflow_charge_power_limit_w(state("kW", 0.1, 0.64, 0.01)) == 640
    assert ecoflow_soc_control_range(state("%", 5, 100, 1)) == (5, 100, 1)
    with pytest.raises(ValueError, match="W or kW"):
        ecoflow_charge_power_limit_w(state("A", 1, 10, 1))
    with pytest.raises(ValueError, match="percent"):
        ecoflow_soc_control_range(state("W", 0, 100, 1))
