"""Probe: long horizon + low load -> does terminal value kill evening discharge?"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
import custom_components.house_battery.planner as P
from custom_components.house_battery.models import PlannerSettings, PriceSlot, Action
from custom_components.house_battery.planner import optimize

TZ = timezone(timedelta(hours=2))

today = [1.25358,1.47411,1.423369,1.397672,1.326187,1.308432,1.316842,1.297125,1.339456,1.316281,1.311983,1.288996,1.243394,1.245731,1.2262,1.212744,1.215735,1.217417,1.219659,1.268064,1.30077,1.359173,1.375526,1.437012,1.459172,1.649613,1.755673,1.885001,1.897896,1.932471,1.983491,1.957233,1.999658,2.01246,1.982277,1.984426,1.982651,1.925462,1.866779,1.808002,1.799311,1.751841,1.660078,1.572801,1.679795,1.679141,1.60532,1.591957,1.644567,1.604385,1.582613,1.580557,1.57738,1.569343,1.556635,1.551963,1.569811,1.55813,1.562896,1.562335,1.47188,1.481785,1.515426,1.536357,1.561494,1.580837,1.639801,1.816599,2.106623,2.300989,2.505073,2.709063,2.457229,2.728686,2.915389,3.141994,3.01631,3.142367,3.185259,3.379344,3.233476,3.170027,3.055651,3.004723,2.613404,2.512577,2.451464,2.322977,2.427636,2.27261,2.238503,2.162999,2.20019,2.119454,2.094878,2.055257]

tomorrow = [2.141868,2.023006,1.942176,1.894706,1.937224,1.906854,1.902742,1.889753,1.864149,1.851908,1.847143,1.812568,1.833406,1.842751,1.879194,1.88153,1.909564,1.898537,1.891062,1.852469,1.875176,1.886389,2.105985,2.385012,2.29214,2.602378,2.601724,2.874116,2.749647,2.890469,2.960646,2.740676,3.118848,2.822908,2.658445,2.276815,2.568084,2.262518,2.136554,1.839586,2.072918,1.90967,1.757822,1.660265,1.659424,1.551215,1.367876,1.323303,1.476085,1.348813,1.285738,1.208552,1.337133,1.298259,1.247893,1.170053,1.127816,1.201637,1.16183,1.222662,1.0867,1.099408,1.403852,1.570184,1.507296,1.70839,1.887243,2.123379,2.182687,2.490215,2.614964,2.921931,2.571886,2.831196,3.009208,3.346171,3.409807,3.53792,3.634262,3.494281,3.208433,3.068733,2.990519,2.894551,2.646671,2.525099,2.402032,2.257659,2.419973,2.31139,2.250277,2.179633,2.229906,2.167765,2.055818,1.950319]

settings = PlannerSettings(
    capacity_wh=1958.0, reserve_soc=20.0, target_soc=90.0,
    charge_power_w=1200.0, discharge_power_w=800.0,
    round_trip_efficiency=0.85, degradation_cost_dkk_per_kwh=0.35,
    minimum_profit_dkk_per_kwh=0.75, switching_penalty_dkk=0.05,
)

def build_horizon(days):
    base_pattern = today + tomorrow
    slots = []
    t = datetime(2026, 9, 24, 0, 0, tzinfo=TZ)
    for i in range(days * 96):
        p = base_pattern[i % 192]
        e = t + timedelta(minutes=15)
        slots.append(PriceSlot(t, e, p, expected_load_wh=LOAD_W))
        t = e
    return slots

LOAD_W = float(sys.argv[1]) if len(sys.argv) > 1 else 107.0
TERMINAL_OVERRIDE = float(sys.argv[4]) if len(sys.argv) > 4 else None
if TERMINAL_OVERRIDE is not None:
    P._terminal_price = lambda slots: TERMINAL_OVERRIDE
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
SOC = float(sys.argv[3]) if len(sys.argv) > 3 else 49.0
now = datetime(2026, 9, 24, 13, 7, 0, tzinfo=TZ)

slots = build_horizon(DAYS)
eff = TERMINAL_OVERRIDE if TERMINAL_OVERRIDE else P._terminal_price(slots)
print(f"  (effective terminal used: {eff:.3f})")
plan = optimize(slots, now=now, soc=SOC, settings=settings,
                current_action=Action.GRID)

print(f"=== DAYS={DAYS} ({len(plan.slots)} slots) SOC={SOC} LOAD_W={LOAD_W} (0.25 kWh/15min) ===")
print(f"terminal_price={plan.terminal_price_dkk_per_kwh:.4f} cost={plan.expected_cost_dkk:.2f} savings={plan.expected_savings_dkk:.2f} terminal_value={plan.terminal_value_dkk:.2f} realized={plan.realized_savings_dkk:.2f} thr={plan.battery_throughput_kwh:.3f}")
print(f"reason={plan.reason}")
# summarize key windows
def block_summary():
    lines = []
    last = None
    for s in plan.slots:
        if s.action is not last:
            lines.append(f"{s.start.strftime('%m-%d %H:%M')} {s.action.value}")
            last = s.action
    return "  ->  ".join(lines)
print("  plan:", block_summary())
# also print a few evening-day2 slots
for s in plan.slots:
    if s.start.date().day==25 and 18 <= s.start.hour:
        print(f"    {s.start.strftime('%m-%d %H:%M')} {s.action.value} price {s.price:.2f} grid {s.grid_import_wh/1000:.3f} dis {s.battery_discharge_wh/1000:.3f}")
# print first and last few
print("  first:", plan.slots[0].start.strftime('%m-%d %H:%M'), plan.slots[0].action.value, "soc", round(plan.slots[0].soc_start,1), round(plan.slots[0].soc_end,1))
print("  last :", plan.slots[-1].end.strftime('%m-%d %H:%M'), plan.slots[-1].action.value, "soc", round(plan.slots[-1].soc_start,1), round(plan.slots[-1].soc_end,1))
