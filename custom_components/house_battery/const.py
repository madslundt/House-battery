"""Constants for the House Battery integration."""

from datetime import timedelta

DOMAIN = "house_battery"
NAME = "House Battery"
PLATFORMS = ["sensor", "binary_sensor", "number", "switch", "button", "select"]
UPDATE_INTERVAL = timedelta(minutes=1)
# The HA Store *major* version deliberately stays at 1 across the schema-v2
# rewrite. HA 2026.x has no ``migrate_func`` on its Store, so bumping the
# major version would activate HA's own storage-migration path, which raises
# ``NotImplementedError`` (and, on a version mismatch, refuses to read the data
# at all) - i.e. upgrading a user would lose access to their state entirely.
# Instead the payload's ``schema_version`` field carries the semantic version.
# RuntimeState.from_dict reads existing state regardless and applies the
# reset-on-load migration itself: battery-learning and flow-accounting evidence
# produced before this model used the (incorrect) FBP ``Discharge`` telemetry,
# so those totals are discarded while settings, scheduled loads, forecast
# evidence and the manual mode preference are preserved. The on-disk file
# identity (STORAGE_KEY) is unchanged, so state is read and migrated, not
# discarded wholesale.
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
# The direct adapter's canonical connected-load source is the FOSSiBOT
# smart-load total. This versioned identifier intentionally invalidates any
# load profile learned from the previous off-grid-load source, so the learner
# is reset on load.
DIRECT_LOAD_SOURCE = "direct:off_grid_total:v3"

# A direct local connection is the default setup path. The entity keys above
# remain supported for entries created before direct local support existed.
DEFAULT_PORT = 8080

ACTION_CHARGE = "charge"
ACTION_GRID = "grid"
ACTION_BATTERY = "battery"
ACTION_SAFE = "safe"

# Manual operating override for the storage controller. `auto` follows the
# optimizer plan; the other options force the commanded battery action
# regardless of price so the physical functions can be tested in isolation.
# `charge` raises the native ceiling to target_soc (never above it) and
# `battery` lowers the native floor to reserve_soc (never below it), so an
# override can never breach the configured SOC limits.
OVERRIDE_AUTO = "auto"
OVERRIDE_CHARGE = "charge"
OVERRIDE_BATTERY = "battery"
OVERRIDE_GRID = "grid"
OVERRIDE_OPTIONS = (
    OVERRIDE_AUTO,
    OVERRIDE_CHARGE,
    OVERRIDE_BATTERY,
    OVERRIDE_GRID,
)
# Runtime storage key for the persistent override selection.
CONF_MODE_OVERRIDE = "mode_override"

MODE_CHARGE = "Charge"
MODE_GRID = "Idle"
MODE_BATTERY = "Self-Gen/Zero Export"
MODE_SAFE = "Self-Gen/Zero Export"

# Map a manual override selection onto the planner action it forces.
OVERRIDE_TO_ACTION = {
    OVERRIDE_CHARGE: ACTION_CHARGE,
    OVERRIDE_BATTERY: ACTION_BATTERY,
    OVERRIDE_GRID: ACTION_GRID,
}

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

# Canonical power-flow thresholds. ``FLOW_NOISE_FLOOR_W`` is the small bound
# used when deriving physical flow (meter jitter around zero export is
# ignored). ``FLOW_UI_ACTIVE_THRESHOLD_W`` is the slightly larger bound used
# to classify the observed power source, so ordinary measurement noise does
# not make the Power source entity jump between states. ``EXPORT_SAFETY_W`` is
# the grid-meter export level that is treated as a real export fault rather
# than noise; export is only ever observed at the meter, never commanded.
FLOW_NOISE_FLOOR_W = 5.0
FLOW_UI_ACTIVE_THRESHOLD_W = 20.0
EXPORT_SAFETY_W = 10.0
