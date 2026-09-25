"""Reproduce the planner output with the user's known prices."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from custom_components.house_battery.models import PlannerSettings, PriceSlot, Action
from custom_components.house_battery.planner import optimize

TZ = timedelta(hours=2)

today = [
1.25358,1.47411,1.423369,1.397672,1.326187,1.308432,1.316842,1.297125,1.339456,1.316281,1.311983,1.288996,1.243394,1.245731,1.2262,1.212744,1.215735,1.217417,1.219659,1.268064,1.30077,1.359173,1.375526,1.437012,1.459172,1.649613,1.755673,1.885001,1.897896,1.932471,1.983491,1.957233,1.999658,2.01246,1.982277,1.984426,1.982651,1.925462,1.866779,1.808002,1.799311,1.751841,1.660078,1.572801,1.679795,1.679141,1.60532,1.591957,1.644567,1.604385,1.582613,1.580557,1.57738,1.569343,1.556635,1.551963,1.569811,1.55813,1.562896,1.562335,1.47188,1.481785,1.515426,1.536357,1.561494,1.580837,1.639801,1.816599,2.106623,2.300989,2.505073,2.709063,2.457229,2.728686,2.915389,3.141994,3.01631,3.142367,3.185259,3.379344,3.233476,3.170027,3.055651,3.004723,2.613404,2.512577,2.451464,2.322977,2.427636,2.27261,2.238503,2.162999,2.20019,2.119454,2.094878,2.055257
]

tomorrow = [
2.141868,2.023006,1.942176,1.894706,1.937224,1.906854,1.902742,1.889753,1.864149,1.851908,1.847143,1.812568,1.833406,1.842751,1.879194,1.88153,1.909564,1.898537,1.891062,1.852469,1.875176,1.886389,2.105985,2.385012,2.29214,2.602378,2.601724,2.874116,2.749647,2.890469,2.960646,2.740676,3.118848,2.822908,2.658445,2.276815,2.568084,2.262518,2.136554,1.839586,2.072918,1.90967,1.757822,1.660265,1.659424,1.551215,1.367876,1.323303,1.476085,1.348813,1.285738,1.208552,1.337133,1.298259,1.247893,1.170053,1.127816,1.201637,1.16183,1.222662,1.0867,1.099408,1.403852,1.570184,1.507296,1.70839,1.887243,2.123379,2.182687,2.490215,2.614964,2.921931,2.571886,2.831196,3.009208,3.346171,3.409807,3.53792,3.634262,3.494281,3.208433,3.068733,2.990519,2.894551,2.646671,2.525099,2.402032,2.257659,2.419973,2.31139,2.250277,2.179633,2.229906,2.167765,2.055818,1.950319
]

def build_slots(prices, base_date, source="known"):
    slots = []
    t = base_date
    for p in prices:
        e = t + timedelta(minutes=15)
        slots.append(PriceSlot(t, e, p, source=source))
        t = e
    return slots

base = datetime(2026, 9, 24, tzinfo=None) - TZ  # naive local; slots carry +02:00
# Build naive-aware datetimes already offset
def naive_to_tz(dt):
    return dt.replace(tzinfo=__import__("datetime").timezone(TZ))

slots = []
import math as _math
LOAD_BASE = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
# Household load shape (W): quiet night, morning + evening peaks
def load_for_hour(h, m=0):
    # quiet night ~300W, mild morning peak ~08:00, strong evening peak ~19:00
    if h < 6:
        return 300.0
    if h < 10:
        return 300 + 550*((h - 6) / 4)   # -> ~850 at 10:00
    if h < 16:
        return 500.0
    if h < 22:
        return 500 + 1100*((h - 16) / 6)  # -> ~1600 at 22:00
    return 400.0

slots = []
t = datetime(2026,9,24,0,0)
for p in today:
    e = t + timedelta(minutes=15)
    load_wh = load_for_hour(t.hour, t.minute)*(e-t).total_seconds()/3600
    slots.append(PriceSlot(t.replace(tzinfo=__import__("datetime").timezone(TZ)), e.replace(tzinfo=__import__("datetime").timezone(TZ)), p, expected_load_wh=load_wh))
    t = e
t = datetime(2026,9,25,0,0)
for p in tomorrow:
    e = t + timedelta(minutes=15)
    load_wh = load_for_hour(t.hour, t.minute)*(e-t).total_seconds()/3600
    slots.append(PriceSlot(t.replace(tzinfo=__import__("datetime").timezone(TZ)), e.replace(tzinfo=__import__("datetime").timezone(TZ)), p, expected_load_wh=load_wh))
    t = e

# now = 13:07 local on 09-24
now = datetime(2026,9,24,13,7,0, tzinfo=__import__("datetime").timezone(TZ))

settings = PlannerSettings(
    capacity_wh=1958.0,
    reserve_soc=20.0,
    target_soc=90.0,
    charge_power_w=1200.0,
    discharge_power_w=800.0,
    round_trip_efficiency=0.85,
    degradation_cost_dkk_per_kwh=0.35,
    minimum_profit_dkk_per_kwh=0.75,
    switching_penalty_dkk=0.05,
    minimum_mode_minutes=30,
    maximum_transitions=4,
)

def load_w_for(hour):
    # rough daily load shape (W): low night, morning+evening peaks
    # crude
    import math
    return 300 + 200*math.sin((hour-6)/24*2*3.14159)

SOC = float(sys.argv[1]) if len(sys.argv) > 1 else 49.0
LOAD_W = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0

plan = optimize(
    slots,
    now=now,
    soc=SOC,
    settings=settings,
    current_action=Action.GRID,
    mode_lock_remaining_minutes=0,
    transition_times=[],
)

print(f"SOC={SOC} LOAD_W={LOAD_W}")
print(f"terminal_price={plan.terminal_price_dkk_per_kwh:.4f}")
print(f"expected_cost={plan.expected_cost_dkk:.3f} baseline={plan.baseline_cost_dkk:.3f} savings={plan.expected_savings_dkk:.3f} terminal_value={plan.terminal_value_dkk:.3f} realized={plan.realized_savings_dkk:.3f}")
print(f"throughput_kwh={plan.battery_throughput_kwh:.3f}")
print(f"reason={plan.reason}")
print("----")
for s in plan.slots:
    print(f"{s.start.strftime('%m-%d %H:%M')} -> {s.end.strftime('%m-%d %H:%M')} {s.action.value:8s} soc {s.soc_start:5.2f}->{s.soc_end:5.2f} "
          f"chg {s.battery_charge_wh/1000:5.3f} dis {s.battery_discharge_wh/1000:5.3f} "
          f"load {s.expected_load_wh/1000:5.3f} grid {s.grid_import_wh/1000:6.3f} "
          f"cost {s.interval_cost_dkk:6.3f} base {s.baseline_cost_dkk:6.3f} src={s.price_source}")
