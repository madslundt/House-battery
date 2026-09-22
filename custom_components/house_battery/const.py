"""Constants for the House Battery integration."""

from datetime import timedelta

DOMAIN = "house_battery"
NAME = "House Battery"
PLATFORMS = ["sensor", "binary_sensor", "number", "switch", "button"]
UPDATE_INTERVAL = timedelta(minutes=1)
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"

CONF_SOC = "soc_entity"
CONF_LOAD_POWER = "load_power_entity"
CONF_GRID_IMPORT_POWER = "grid_import_power_entity"
CONF_GRID_AVAILABLE = "grid_available_entity"
CONF_BATTERY_CHARGE_POWER = "battery_charge_power_entity"
CONF_BATTERY_DISCHARGE_POWER = "battery_discharge_power_entity"
CONF_FAULT = "fault_entity"
CONF_ONLINE = "online_entity"
CONF_OPERATING_MODE = "operating_mode_entity"
CONF_CHARGE_POWER_CONTROL = "charge_power_control_entity"
CONF_DISCHARGE_POWER_CONTROL = "discharge_power_control_entity"
CONF_MIN_SOC_CONTROL = "minimum_soc_control_entity"
CONF_MAX_SOC_CONTROL = "maximum_soc_control_entity"
CONF_PRICE_ENTITIES = "price_entities"
# `CONF_PRICE_FORECAST_ENTITY` remains readable for entries created before
# forecast comparisons supported more than one source.
CONF_PRICE_FORECAST_ENTITY = "price_forecast_entity"
CONF_PRICE_FORECAST_ENTITIES = "price_forecast_entities"
CONF_COMMISSIONED = "commissioned"
# The complete Storage_list off-grid total is the direct adapter's only
# battery-served load model. Other local readings remain diagnostic only.
DIRECT_LOAD_FIELD = "off_grid_load_power_total_w"

# A direct local connection is the default setup path. The entity keys above
# remain supported for entries created before direct local support existed.
DEFAULT_PORT = 8080

ACTION_CHARGE = "charge"
ACTION_GRID = "grid"
ACTION_BATTERY = "battery"
ACTION_SAFE = "safe"

MODE_CHARGE = "Charge"
MODE_GRID = "Idle"
MODE_BATTERY = "Self-Gen/Zero Export"
MODE_SAFE = "Self-Gen/Zero Export"

DEFAULT_SETTINGS: dict[str, float] = {
    "capacity_kwh": 1.958,
    "absolute_min_soc": 10.0,
    "reserve_soc": 20.0,
    "target_soc": 90.0,
    "opportunistic_target_soc": 100.0,
    "charge_power_w": 1200.0,
    "discharge_power_w": 800.0,
    "round_trip_efficiency": 85.0,
    "degradation_cost_dkk_per_kwh": 0.35,
    "minimum_profit_dkk_per_kwh": 0.75,
    "extra_storage_spread_dkk_per_kwh": 2.0,
    "extra_storage_cheap_window_minutes": 30.0,
    "switching_penalty_dkk": 0.05,
    "minimum_mode_minutes": 30.0,
    "maximum_transitions_per_day": 4.0,
    "cycle_life": 6000.0,
    "forecast_uncertainty_dkk_per_kwh": 0.25,
}

SETTING_LIMITS: dict[str, tuple[float, float, float, str | None]] = {
    "capacity_kwh": (0.5, 20.0, 0.001, "kWh"),
    "absolute_min_soc": (0.0, 50.0, 1.0, "%"),
    "reserve_soc": (5.0, 80.0, 1.0, "%"),
    "target_soc": (20.0, 100.0, 1.0, "%"),
    "opportunistic_target_soc": (20.0, 100.0, 1.0, "%"),
    "charge_power_w": (100.0, 1200.0, 50.0, "W"),
    "discharge_power_w": (100.0, 800.0, 50.0, "W"),
    "round_trip_efficiency": (50.0, 100.0, 1.0, "%"),
    "degradation_cost_dkk_per_kwh": (0.0, 5.0, 0.01, "DKK/kWh"),
    "minimum_profit_dkk_per_kwh": (0.0, 10.0, 0.05, "DKK/kWh"),
    "extra_storage_spread_dkk_per_kwh": (0.0, 20.0, 0.05, "DKK/kWh"),
    "extra_storage_cheap_window_minutes": (15.0, 240.0, 15.0, "min"),
    "switching_penalty_dkk": (0.0, 5.0, 0.01, "DKK"),
    "minimum_mode_minutes": (15.0, 120.0, 15.0, "min"),
    "maximum_transitions_per_day": (1.0, 12.0, 1.0, None),
    "cycle_life": (500.0, 15000.0, 100.0, "cycles"),
    "forecast_uncertainty_dkk_per_kwh": (0.0, 10.0, 0.01, "DKK/kWh"),
}

TELEMETRY_STALE_AFTER = timedelta(minutes=5)
# Forecasts remain optional and must be fresh; this conservative, fixed policy
# avoids exposing a tuning knob that can accidentally accept obsolete prices.
FORECAST_MAX_AGE = timedelta(minutes=180)
# A PS240/FBP1200 may briefly disappear while it renews its local network
# session.  During this bounded window the coordinator stops writes but keeps
# the user's automatic-control authorization intact.  Invalid telemetry and
# every non-local-TCP health failure still fail safe immediately.
LOCAL_TCP_RECOVERY_GRACE = timedelta(minutes=2)
DECISION_HISTORY_LIMIT = 500
LEDGER_HISTORY_LIMIT = 96 * 62
