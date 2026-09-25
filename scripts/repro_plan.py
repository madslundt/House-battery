"""Print the plan produced by the attached September 25–26 regression fixture."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.dailyplan import merge_adjacent_blocks
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.planner import optimize


ROOT = Path(__file__).resolve().parents[1]
rows = json.loads(
    (ROOT / "tests/fixtures/prices_2026-09-25_26.json").read_text()
)
slots = [
    PriceSlot(
        datetime.fromisoformat(row["start"]),
        datetime.fromisoformat(row["end"]),
        row["price"],
        expected_load_wh=110 * (
            datetime.fromisoformat(row["end"])
            - datetime.fromisoformat(row["start"])
        ).total_seconds()
        / 3600,
    )
    for row in rows
]
now = datetime.fromisoformat("2026-09-25T16:55:00+02:00")
settings = PlannerSettings(
    capacity_wh=1956,
    reserve_soc=20,
    target_soc=90,
    charge_power_w=800,
    discharge_power_w=800,
    round_trip_efficiency=0.85,
    degradation_cost_dkk_per_kwh=0.35,
    minimum_profit_dkk_per_kwh=0.75,
    switching_penalty_dkk=0.05,
    energy_step_wh=5,
)
plan = optimize(
    slots,
    now=now,
    soc=48,
    settings=settings,
    current_action=Action.GRID,
)

print(
    f"cost={plan.expected_cost_dkk:.3f} DKK "
    f"baseline={plan.baseline_cost_dkk:.3f} DKK "
    f"savings={plan.expected_savings_dkk:.3f} DKK"
)
print(f"battery throughput={plan.battery_throughput_kwh:.3f} kWh")
for block in merge_adjacent_blocks(plan.slots):
    print(
        f"{block.start.isoformat()} -> {block.end.isoformat()} "
        f"{block.action.value}"
    )
