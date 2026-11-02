"""Raw provider evidence stays independent of calibrated planning inputs."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.forecast import ForecastAccuracy
from house_battery.models import PriceSlot
from test_house_battery_cutoff_gate import coordinator

HOUR = timedelta(hours=1)


def test_long_source_horizons_record_the_first_usable_calibration_vintage():
    target = datetime(2026, 11, 5, 12, tzinfo=UTC)
    forecast = PriceSlot(target, target + HOUR, 2)
    first = target - HOUR * 72 - timedelta(seconds=30)
    later = first + timedelta(minutes=1)
    accuracy = ForecastAccuracy()
    accuracy.record_forecasts([forecast], now=first)
    assert accuracy.outstanding == {}
    # Earlier storage versions could retain this unusable >72 h vintage.
    accuracy.outstanding["old-window"] = {
        "start": target.isoformat(), "end": (target + HOUR).isoformat(),
        "price": 2, "issued_at": first.isoformat(),
    }
    accuracy.prune(later)
    assert accuracy.outstanding == {}
    accuracy.record_forecasts([forecast], now=later)
    assert len(accuracy.outstanding) == 1
    accuracy.score_actual_prices(
        [PriceSlot(target, target + HOUR, 1)],
        uncertainty_dkk_per_kwh=.35, now=target + HOUR,
    )
    assert accuracy.error_samples[0]["issued_at"] == later.isoformat()
    assert (target - later).total_seconds() / 3600 < 72


def _evidence(now: datetime) -> ForecastAccuracy:
    rows = []
    for index in range(168):
        start = now.replace(minute=0, second=0, microsecond=0) - HOUR * (168 - index)
        rows.append({
            "start": start.isoformat(), "end": (start + HOUR).isoformat(),
            "issued_at": (start - HOUR * 36).isoformat(),
            "scored_at": (start + HOUR).isoformat(), "error_dkk_per_kwh": .5,
        })
    return ForecastAccuracy(error_samples=rows)


async def _inputs(*, adaptive: bool, peer: bool = False):
    coord, client, hass, _ = await coordinator()
    now = datetime.now(UTC)
    coord._local_snapshot = await client.async_snapshot()
    coord._local_controls = await client.async_read_controls()
    coord.runtime.forecast_enabled = True
    coord.runtime.adaptive_enabled = adaptive
    coord.runtime.settings["forecast_uncertainty_dkk_per_kwh"] = .35
    sources = ["sensor.biased_forecast"] + (["sensor.peer_forecast"] if peer else [])
    coord.entry.options["price_forecast_entities"] = sources
    known = hass.states.get("sensor.prices")
    end = datetime.fromisoformat(known.attributes["prices"][-1]["end"])
    for source in sources:
        price = 2.6 if source == "sensor.peer_forecast" else 3
        rows = [{"start": (end + HOUR * i).isoformat(),
                 "end": (end + HOUR * (i + 1)).isoformat(), "price": price}
                for i in range(80)]
        hass.states.async_set(source, str(price), {"prices": rows})
    accuracy = _evidence(now)
    coord.runtime.forecast_accuracies[sources[0]] = accuracy
    return coord, now, accuracy, [dict(row) for row in accuracy.error_samples]


@pytest.mark.parametrize("adaptive", [False, True])
def test_calibration_requires_opt_in_and_never_rewrites_provider_evidence(adaptive):
    async def scenario():
        coord, now, accuracy, original = await _inputs(adaptive=adaptive)
        slots = coord._price_slots(now)
        future = next(slot for slot in slots if slot.start <= now + HOUR * 36 < slot.end)
        assert future.price == pytest.approx(2.6 if adaptive else 3)
        assert accuracy.error_samples == original
        assert [slot.price for slot in slots if slot.source == "known"] == [4, 4, 1, 1]
        diagnostic = coord._forecast_sources["sensor.biased_forecast"]["calibration"]
        assert diagnostic["status"] == ("active" if adaptive else "disabled")
        if adaptive:
            # The raw source's .5 allowance is retained around the original
            # price: correcting its mean cannot invent a better buying bound.
            assert future.uncertainty_dkk_per_kwh >= .9
            assert future.charge_price_dkk_per_kwh >= 3.5 - 1e-9

    asyncio.run(scenario())


def test_corrected_peers_are_compared_on_the_same_planning_basis():
    async def scenario():
        coord, now, _, _ = await _inputs(adaptive=True, peer=True)
        slots = coord._price_slots(now)
        future = next(slot for slot in slots if slot.start <= now + HOUR * 36 < slot.end)
        assert future.price == pytest.approx(2.6)
        assert future.uncertainty_dkk_per_kwh == pytest.approx(.9)
        assert coord._forecast_planning_source == "sensor.biased_forecast"

    asyncio.run(scenario())
