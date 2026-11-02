"""A learned forecast must support a battery opportunity beyond terminal value."""

import asyncio
from datetime import UTC, datetime, timedelta

from test_house_battery_cutoff_gate import coordinator
from house_battery.models import Action, PlannerSettings, PriceSlot
from house_battery.policy import has_forecast_backed_movement

NOW = datetime(2026, 11, 2, tzinfo=UTC)
HOUR = timedelta(hours=1)
SETTINGS = PlannerSettings(2000, 20, 90, 800, 800, .85, .35, .75, .05, energy_step_wh=25)


def _inputs(*, peak: bool):
    known = [PriceSlot(NOW + HOUR * i, NOW + HOUR * (i + 1),
                       1 if peak else .1 if i == 0 else 1, 100)
             for i in range(12)]
    tail = [PriceSlot(NOW + HOUR * i, NOW + HOUR * (i + 1),
                      6 if peak and i < 24 else 1, 100, source="forecast",
                      uncertainty_dkk_per_kwh=.25 if peak else .9)
            for i in range(12, 72)]
    return known, tail


def test_terminal_only_changes_preserve_the_trusted_price_plan():
    async def scenario():
        coord, _, _, _ = await coordinator()
        known, tail = _inputs(peak=False)
        arguments = dict(now=NOW, soc=20, settings=SETTINGS, current_action=Action.GRID,
                         opportunistic_enabled=False)
        baseline = coord._optimize_with_storage_policy(known, **arguments)
        candidate = coord._optimize_with_storage_policy(known + tail, **arguments)
        result = coord._optimize_with_storage_policy(
            known + tail, learned_tail_start=tail[0].start, **arguments,
        )

        assert baseline.plan.current_action is Action.CHARGE
        assert candidate.plan.current_action is Action.GRID
        assert not has_forecast_backed_movement(candidate.plan, tail[0].start, 25)
        assert result.plan.as_dict() == baseline.plan.as_dict()
        assert result.learned_control_status == "shadow"

    asyncio.run(scenario())


def test_credible_forecast_peak_still_prepares_the_battery_at_known_low_prices():
    async def scenario():
        coord, _, _, _ = await coordinator()
        known, tail = _inputs(peak=True)
        result = coord._optimize_with_storage_policy(
            known + tail, now=NOW, soc=20, settings=SETTINGS, current_action=Action.GRID,
            opportunistic_enabled=False, learned_tail_start=tail[0].start,
        )

        assert result.learned_control_status == "active"
        assert any(slot.battery_charge_wh > 0 for slot in result.plan.slots[:12])
        assert any(slot.battery_discharge_wh > 0 for slot in result.plan.slots[12:24])
        assert result.plan.slots[-1].end == NOW + HOUR * 72

    asyncio.run(scenario())
