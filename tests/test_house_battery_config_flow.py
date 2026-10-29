"""Configuration changes replace optional bindings without reviving originals."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.config_flow import Fbp1200OptionsFlow
from house_battery.const import (
    CONF_ACCOUNTING_LOAD_POWER,
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_CHARGE_POWER_CONTROL,
    CONF_COMMISSIONED,
    CONF_DISCHARGE_POWER_CONTROL,
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


@pytest.mark.parametrize("direct_local", [True, False])
def test_options_can_remove_optional_bindings_selected_during_setup(direct_local: bool) -> None:
    async def scenario() -> None:
        submitted = {
            CONF_GRID_AVAILABLE: "binary_sensor.grid", CONF_PRICE_ENTITIES: ["sensor.price"],
            CONF_COMMISSIONED: True,
        }
        optional = {
            CONF_GRID_ISOLATION_SWITCH: "switch.old_plug",
            CONF_PRICE_FORECAST_ENTITIES: ["sensor.old_forecast"],
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
        validated = form["data_schema"](submitted)
        result = await flow.async_step_init(validated)
        merged = {**original, **result["data"]}

        assert all(not merged.get(key) for key in optional)
        assert not merged.get(CONF_PRICE_FORECAST_ENTITY)
        assert merged[CONF_PRICE_ENTITIES] == ["sensor.price"]
        assert merged[CONF_COMMISSIONED]

    asyncio.run(scenario())
