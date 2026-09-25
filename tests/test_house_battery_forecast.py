"""Tests for external-price forecast safety and evidence."""

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.const import (
    CONF_LOAD_POWER,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_FORECAST_ENTITIES,
)
from house_battery.coordinator import Fbp1200Coordinator
from house_battery.forecast import (
    ForecastAccuracy,
    assess_external_forecast,
    detect_extreme_price_movement,
    extend_known_horizon,
)
from house_battery.forecast_plan import ForecastPlan
from house_battery.models import Action, PriceSlot
from house_battery.price import extract_rows, normalize_price_rows
from house_battery.runtime import RuntimeState


def _slot(hour: int, price: float) -> PriceSlot:
    start = datetime(2026, 1, 1, hour, tzinfo=UTC)
    return PriceSlot(start, start + timedelta(hours=1), price)


def _stromligning_prices(
    start: datetime,
    *,
    intervals: int,
    cadence: timedelta,
    default_price: float,
) -> list[dict[str, float | str]]:
    """Produce the documented ``prices`` rows used by Strømligning VAT sensors."""
    return [
        {
            "start": (start + cadence * index).isoformat(),
            "end": (start + cadence * (index + 1)).isoformat(),
            "price": default_price,
        }
        for index in range(intervals)
    ]


class _Entry:
    """Small ConfigEntry seam for forecast-coordinator coverage."""

    entry_id = "forecast-test"
    title = "Forecast test"

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.options: dict[str, object] = {}

    def async_on_unload(self, callback: object) -> None:
        del callback


def test_stromligning_vat_price_rows_keep_their_explicit_quarter_hour_cadence() -> (
    None
):
    """Current-price and tomorrow-available attributes use a supported format."""
    start = datetime.fromisoformat("2026-09-22T13:45:00+02:00")
    attributes = {
        "forecast_data": False,
        "prices": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=15)).isoformat(),
                "price": 1.297170,
            },
            {
                "start": (start + timedelta(minutes=15)).isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
                "price": 6.619447,
            },
        ],
    }

    slots = normalize_price_rows(extract_rows(attributes))

    assert [slot.hours for slot in slots] == [0.25, 0.25]
    assert [slot.price for slot in slots] == [1.297170, 6.619447]


def test_stromligning_current_tomorrow_and_forecast_entities_extend_the_plan() -> (
    None
):
    """Exercise the live entity shape through parsing, forecast validation and planning."""

    async def scenario() -> None:
        # The two known-price entities together run until midnight on 23 Sep,
        # precisely where the hourly forecast entity begins.  This mirrors the
        # installed Strømligning VAT sources while keeping the test deterministic.
        now = datetime.fromisoformat("2026-09-21T20:30:00+02:00")
        midnight = datetime.fromisoformat("2026-09-22T00:00:00+02:00")
        forecast_start = datetime.fromisoformat("2026-09-23T00:00:00+02:00")
        current = _stromligning_prices(
            now,
            intervals=14,
            cadence=timedelta(minutes=15),
            default_price=2.807834,
        )
        tomorrow = _stromligning_prices(
            midnight,
            intervals=96,
            cadence=timedelta(minutes=15),
            default_price=2.20,
        )
        tomorrow[55]["price"] = 1.297170  # 13:45–14:00, low-price window
        tomorrow[77]["price"] = 6.619447  # 19:15–19:30, high-price window
        forecasts = _stromligning_prices(
            forecast_start,
            intervals=144,
            cadence=timedelta(hours=1),
            default_price=2.93,
        )
        hass = HomeAssistant("/tmp")
        entry = _Entry(
            {
                CONF_PRICE_ENTITIES: [
                    "sensor.stromligning_current_price_vat",
                    "binary_sensor.stromligning_tomorrow_available_vat",
                ],
                CONF_LOAD_POWER: "sensor.load",
                CONF_PRICE_FORECAST_ENTITIES: [
                    "sensor.stromligning_forecasts_vat"
                ],
            }
        )
        coordinator = Fbp1200Coordinator(hass, entry)
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.load", "110")
        hass.states.async_set(
            "sensor.stromligning_current_price_vat", "2.807834", {"prices": current}
        )
        hass.states.async_set(
            "binary_sensor.stromligning_tomorrow_available_vat",
            "on",
            {"forecast_data": "false", "prices": tomorrow},
        )
        hass.states.async_set(
            "sensor.stromligning_forecasts_vat",
            forecast_start.isoformat(),
            {"prices": forecasts},
        )

        slots = coordinator._price_slots(now.astimezone(UTC))

        # Known prices drive planning; forecasts are indicators only.
        assert len(slots) == 110
        assert all(slot.source == "known" for slot in slots)
        assert coordinator._forecast_status == "used_as_indicator"
        assert coordinator._forecast_used_slot_count == 0
        # Extreme price indicator detects the 6.62 spike vs ~2.9 baseline.
        assert coordinator._extreme_price_is_extreme is True
        assert coordinator._extreme_price_ratio > 2.0
        assert slots[69].price == 1.297170
        assert slots[91].price == 6.619447
        assert all(slot.hours == 0.25 for slot in slots)

        # The optimizer receives only known-price slots; extreme-price
        # detection runs independently on the forecast data.
        # We verify the data pipeline, not the full optimisation logic
        # (which is covered by planner tests).
        assert slots[0].source == "known"
        assert slots[69].price == 1.297170  # cheap window
        assert slots[91].price == 6.619447  # expensive spike

    asyncio.run(scenario())


def test_price_entity_flagged_forecast_is_not_treated_as_known() -> None:
    """A price entity with forecast_data:true contributes no known-price slots."""

    async def scenario() -> None:
        now = datetime.fromisoformat("2026-09-22T09:30:00+02:00")
        start = datetime.fromisoformat("2026-09-22T09:00:00+02:00")
        known = _stromligning_prices(
            start, intervals=3, cadence=timedelta(hours=1), default_price=2.0
        )
        forecast_start = datetime.fromisoformat("2026-09-23T00:00:00+02:00")
        forecast = _stromligning_prices(
            forecast_start, intervals=4, cadence=timedelta(hours=1), default_price=9.9
        )
        hass = HomeAssistant("/tmp")
        entry = _Entry(
            {
                CONF_PRICE_ENTITIES: [
                    "sensor.stromligning_current_price_vat",
                    "sensor.stromligning_prices_tomorrow_vat",
                ],
                CONF_LOAD_POWER: "sensor.load",
                CONF_PRICE_FORECAST_ENTITIES: [],
            }
        )
        coordinator = Fbp1200Coordinator(hass, entry)
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.load", "110")
        hass.states.async_set(
            "sensor.stromligning_current_price_vat", "2.0", {"prices": known}
        )
        hass.states.async_set(
            "sensor.stromligning_prices_tomorrow_vat",
            forecast_start.isoformat(),
            {"forecast_data": "true", "prices": forecast},
        )

        slots = coordinator._price_slots(now.astimezone(UTC))

        # The forecast-flagged entity never contributes known-price slots;
        # only the confirmed-price entity drives planning.
        assert len(slots) == 3
        assert [slot.price for slot in slots] == [2.0, 2.0, 2.0]
        assert all(slot.source == "known" for slot in slots)
        # The flagged entity is routed through the forecast path instead.
        assert coordinator._forecast_source == "sensor.stromligning_prices_tomorrow_vat"

    asyncio.run(scenario())


def test_coordinator_builds_a_separate_forecast_plan_from_forecast_slots() -> None:
    """The forecast-horizon guideline is built, separate from the real plan."""

    async def scenario() -> None:
        now = datetime.fromisoformat("2026-09-22T12:00:00+02:00")
        known_start = datetime.fromisoformat("2026-09-22T09:00:00+02:00")
        # Three hourly *known* prices (baseline ~2.0) ending at local noon.
        current = _stromligning_prices(
            known_start, intervals=3, cadence=timedelta(hours=1), default_price=2.0
        )
        # The forecast entity covers the rest of the day after known prices.
        forecast_start = datetime.fromisoformat("2026-09-22T12:00:00+02:00")
        forecast = _stromligning_prices(
            forecast_start,
            intervals=12,
            cadence=timedelta(hours=1),
            default_price=2.0,
        )
        forecast[0]["price"] = 0.5  # 12:00-13:00, overnight dip -> charge
        forecast[1]["price"] = 0.5  # 13:00-14:00
        forecast[9]["price"] = 4.0  # 21:00-22:00, evening spike -> use battery
        forecast[10]["price"] = 4.0
        hass = HomeAssistant("/tmp")
        entry = _Entry(
            {
                CONF_PRICE_ENTITIES: ["sensor.stromligning_current_price_vat"],
                CONF_LOAD_POWER: "sensor.load",
                CONF_PRICE_FORECAST_ENTITIES: ["sensor.stromligning_forecasts_vat"],
            }
        )
        coordinator = Fbp1200Coordinator(hass, entry)
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.load", "110")
        hass.states.async_set(
            "sensor.stromligning_current_price_vat",
            "2.0",
            {"prices": current},
        )
        hass.states.async_set(
            "sensor.stromligning_forecasts_vat",
            forecast_start.isoformat(),
            {"prices": forecast},
        )

        coordinator._price_slots(now.astimezone(UTC))
        plan = coordinator._forecast_plan

        assert isinstance(plan, ForecastPlan)
        # Judged against the recent known-price baseline.
        assert plan.baseline_price_dkk_per_kwh == 2.0
        # Only considered *after* the known prices end (local noon).
        assert plan.horizon_start == now
        assert plan.horizon_end == datetime.fromisoformat(
            "2026-09-23T00:00:00+02:00"
        )
        # Major dip -> charge, major spike -> use the battery.
        assert [block.recommendation for block in plan.blocks] == [
            Action.CHARGE,
            Action.BATTERY,
        ]
        assert plan.blocks[0].start.hour == 12
        assert plan.blocks[0].end.hour == 14
        assert plan.blocks[1].start.hour == 21
        assert plan.blocks[1].end.hour == 23
        # It is deliberately separate from the real optimizer plan, which
        # ``_price_slots`` never populates.
        assert plan is not coordinator.plan
        assert coordinator.plan is None
        assert plan.summary() != "no major forecast swings detected"

    asyncio.run(scenario())


def test_external_forecast_only_extends_known_horizon_conservatively() -> None:
    """Known prices win and forecast slots are marked with their uncertainty."""
    known = [_slot(0, 1.0)]
    forecast = [_slot(0, 9.0), _slot(1, 0.5), _slot(2, 2.5)]

    merged = extend_known_horizon(known, forecast, uncertainty_dkk_per_kwh=0.25)

    assert [slot.price for slot in merged] == [1.0, 0.5, 2.5]
    assert [slot.source for slot in merged] == ["known", "forecast", "forecast"]
    assert merged[1].uncertainty_dkk_per_kwh == 0.25


def test_hourly_known_prices_and_quarterly_forecasts_keep_their_source_cadence() -> (
    None
):
    """The planner can join a 60-minute known horizon to 15-minute forecasts."""
    known = normalize_price_rows(
        [
            {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T01:00:00+00:00",
                "price": 1.0,
            }
        ]
    )
    forecast_start = datetime(2026, 1, 1, 1, tzinfo=UTC)
    forecast = normalize_price_rows(
        [
            {
                "start": forecast_start + timedelta(minutes=minute),
                "end": forecast_start + timedelta(minutes=minute + 15),
                "price": 0.5,
            }
            for minute in range(0, 60, 15)
        ]
    )

    merged = extend_known_horizon(known, forecast, uncertainty_dkk_per_kwh=0.25)

    assert [slot.hours for slot in merged] == [1.0, 0.25, 0.25, 0.25, 0.25]


def test_missing_end_uses_the_source_cadence_inferred_from_adjacent_starts() -> None:
    """Quarter-hour rows without `end` do not silently become hourly prices."""
    slots = normalize_price_rows(
        [
            {"start": f"2026-01-01T00:{minute:02d}:00+00:00", "price": 1.0}
            for minute in (0, 15, 30)
        ]
    )

    assert [slot.hours for slot in slots] == [0.25, 0.25, 0.25]


def test_forecast_accuracy_scores_forecast_once_actual_price_is_known() -> None:
    """Accuracy reports absolute error, bias, and whether the error fit the buffer."""
    accuracy = ForecastAccuracy()
    accuracy.record_forecasts([_slot(1, 1.20)])

    matched = accuracy.score_actual_prices(
        [_slot(1, 1.00)], uncertainty_dkk_per_kwh=0.25
    )

    assert matched == 1
    assert accuracy.samples == 1
    assert accuracy.mean_absolute_error_dkk_per_kwh == 0.20
    assert accuracy.mean_bias_dkk_per_kwh == 0.20
    assert accuracy.within_uncertainty_pct == 100.0


def test_forecast_accuracy_compares_hourly_forecast_to_quarterly_actual_prices() -> (
    None
):
    """Accuracy is duration-weighted when the two sources use different cadences."""
    accuracy = ForecastAccuracy()
    forecast = _slot(1, 1.20)
    actual_start = forecast.start
    actual = [
        PriceSlot(
            actual_start + timedelta(minutes=15 * index),
            actual_start + timedelta(minutes=15 * (index + 1)),
            price,
        )
        for index, price in enumerate((1.00, 1.10, 1.30, 1.40))
    ]
    accuracy.record_forecasts([forecast])

    matched = accuracy.score_actual_prices(actual, uncertainty_dkk_per_kwh=0.25)

    assert matched == 1
    assert accuracy.samples == 1
    assert accuracy.mean_absolute_error_dkk_per_kwh == 0.0


def test_overlapping_forecasts_keep_independent_accuracy_histories() -> None:
    """The same actual price can score every source that predicted its interval."""
    runtime = RuntimeState()
    source_a = runtime.forecast_accuracy_for("sensor.forecast_a")
    source_b = runtime.forecast_accuracy_for("sensor.forecast_b")
    source_a.record_forecasts([_slot(1, 1.20)])
    source_b.record_forecasts([_slot(1, 0.80)])

    actual = [_slot(1, 1.00)]
    source_a.score_actual_prices(actual, uncertainty_dkk_per_kwh=0.25)
    source_b.score_actual_prices(actual, uncertainty_dkk_per_kwh=0.25)

    assert source_a.samples == source_b.samples == 1
    assert source_a.mean_bias_dkk_per_kwh == 0.20
    assert source_b.mean_bias_dkk_per_kwh == -0.20
    restored = RuntimeState.from_dict(runtime.as_dict())
    assert restored.forecast_accuracy_for("sensor.forecast_a").samples == 1
    assert restored.forecast_accuracy_for("sensor.forecast_b").samples == 1


def test_forecasts_are_indicators_only_and_all_sources_score_independently() -> (
    None
):
    """Forecasts never drive planning; they only feed the extreme-price indicator."""

    async def scenario() -> None:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        known_start = now
        forecast_start = now + timedelta(hours=1)
        hass = HomeAssistant("/tmp")
        entry = _Entry(
            {
                CONF_PRICE_ENTITIES: ["sensor.known"],
                CONF_LOAD_POWER: "sensor.load",
                CONF_PRICE_FORECAST_ENTITIES: [
                    "sensor.forecast_a",
                    "sensor.forecast_b",
                ],
            }
        )
        coordinator = Fbp1200Coordinator(hass, entry)
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.load", "110")
        hass.states.async_set(
            "sensor.known",
            "1.0",
            {
                "prices": [
                    {
                        "start": known_start.isoformat(),
                        "end": forecast_start.isoformat(),
                        "price": 1.0,
                    }
                ]
            },
        )
        for source, price in (("sensor.forecast_a", 1.20), ("sensor.forecast_b", 0.80)):
            hass.states.async_set(
                source,
                str(price),
                {
                    "prices": [
                        {
                            "start": forecast_start.isoformat(),
                            "end": (forecast_start + timedelta(hours=1)).isoformat(),
                            "price": price,
                        }
                    ]
                },
            )

        slots = coordinator._price_slots(now)

        # Only known prices for planning; forecasts are indicators only.
        assert [slot.price for slot in slots] == [1.0]
        assert coordinator._forecast_planning_source is None
        assert coordinator._forecast_sources["sensor.forecast_a"]["status"] == "used_as_indicator"
        assert (
            coordinator._forecast_sources["sensor.forecast_b"]["status"] == "used_as_indicator"
        )

        # When the actual price is published, both previously overlapping
        # predictions are scored against it, not against one another.
        hass.states.async_set(
            "sensor.known",
            "1.0",
            {
                "prices": [
                    {
                        "start": known_start.isoformat(),
                        "end": forecast_start.isoformat(),
                        "price": 1.0,
                    },
                    {
                        "start": forecast_start.isoformat(),
                        "end": (forecast_start + timedelta(hours=1)).isoformat(),
                        "price": 1.0,
                    },
                ]
            },
        )
        coordinator._price_slots(now)

        assert (
            coordinator.runtime.forecast_accuracy_for(
                "sensor.forecast_a"
            ).mean_bias_dkk_per_kwh
            == 0.20
        )
        assert (
            coordinator.runtime.forecast_accuracy_for(
                "sensor.forecast_b"
            ).mean_bias_dkk_per_kwh
            == -0.20
        )

    asyncio.run(scenario())


def test_detect_extreme_price_movement_flags_large_spikes() -> None:
    """Extreme-price detection uses recent known prices as baseline."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    # Order matters: the last 24 slots form the baseline window.
    # 10 low + 1 spike + 32 baseline = 43 total, last 24 are all baseline.
    slots = [
        PriceSlot(now + timedelta(hours=i), now + timedelta(hours=i + 1), 1.00)
        for i in range(10)
    ] + [
        PriceSlot(now + timedelta(hours=10), now + timedelta(hours=11), 6.00)
    ] + [
        PriceSlot(now + timedelta(hours=i), now + timedelta(hours=i + 1), 1.50)
        for i in range(11, 43)
    ]
    result = detect_extreme_price_movement(slots)

    assert result["is_extreme"] is True
    assert result["baseline"] == pytest.approx(1.50)
    assert result["max_future"] == 6.0
    assert result["ratio"] == pytest.approx(4.0)


def test_detect_extreme_price_movement_no_flag_when_prices_are_normal() -> None:
    """No flag when future prices stay within the normal baseline range."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    slots = [
        PriceSlot(now + timedelta(hours=i), now + timedelta(hours=i + 1), 2.00)
        for i in range(12)
    ]
    result = detect_extreme_price_movement(slots)

    assert result["is_extreme"] is False
    assert result["ratio"] == 1.0


def test_invalid_external_forecast_is_rejected_without_affecting_known_prices() -> None:
    """Malformed external forecast data is unusable, not a partial plan input."""
    assessment = assess_external_forecast(
        [
            {
                "start": "2026-01-01T02:00:00+00:00",
                "end": "2026-01-01T03:00:00+00:00",
                "price": 0.50,
            },
            {"start": "not-a-date", "price": "broken"},
        ],
        now=datetime(2026, 1, 1, 1, tzinfo=UTC),
        reported_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        maximum_age=timedelta(hours=3),
    )

    assert assessment.status == "invalid"
    assert not assessment.usable
    assert assessment.slots == ()


def test_stale_external_forecast_is_rejected_even_when_its_rows_are_valid() -> None:
    """Old forecast data cannot extend an otherwise healthy known-price plan."""
    assessment = assess_external_forecast(
        [
            {
                "start": "2026-01-01T02:00:00+00:00",
                "end": "2026-01-01T03:00:00+00:00",
                "price": 0.50,
            }
        ],
        now=datetime(2026, 1, 1, 4, tzinfo=UTC),
        reported_at=datetime(2026, 1, 1, 0, tzinfo=UTC),
        maximum_age=timedelta(hours=3),
    )

    assert assessment.status == "stale"
    assert not assessment.usable
