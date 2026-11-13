"""Tests for external-price forecast safety and evidence."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

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
    forecast_adaptation,
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

        # Known prices remain authoritative and the enabled forecast extends
        # them contiguously with an uncertainty buffer.
        assert len(slots) > 110
        assert all(slot.source == "known" for slot in slots[:110])
        assert all(slot.source == "forecast" for slot in slots[110:])
        assert coordinator._forecast_status == "used"
        assert coordinator._forecast_used_slot_count == len(slots) - 110
        # Extreme price indicator detects the 6.62 spike vs ~2.9 baseline.
        assert coordinator._extreme_price_is_extreme is True
        assert coordinator._extreme_price_ratio > 2.0
        assert slots[69].price == 1.297170
        assert slots[91].price == 6.619447
        assert all(slot.hours == 0.25 for slot in slots[:110])
        assert all(slot.hours == 1.0 for slot in slots[110:-1])
        assert slots[-1].hours == pytest.approx(0.5)

        # Known slots are never overwritten by the extension.
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
        assert coordinator._forecast_plan is not None
        assert coordinator._forecast_slot_count == len(forecast)

    asyncio.run(scenario())


def test_stromligning_tomorrow_repairs_mislabeled_forecast_but_preserves_missing_hour():
    async def scenario() -> None:
        now = datetime(2026, 11, 13, 23, tzinfo=UTC)
        hour = timedelta(hours=1)
        quarter = timedelta(minutes=15)
        hass = HomeAssistant("/tmp")
        tomorrow = "binary_sensor.stromligning_tomorrow_available_vat"
        hass.data[er.DATA_REGISTRY] = SimpleNamespace(
            async_get=lambda entity_id: SimpleNamespace(platform="stromligning") if entity_id == tomorrow else None,
        )
        coordinator = Fbp1200Coordinator(hass, _Entry({
            CONF_PRICE_ENTITIES: ["sensor.prices", tomorrow],
            CONF_LOAD_POWER: "sensor.load",
        }))
        coordinator.runtime.forecast_enabled = True
        hass.states.async_set("sensor.load", "110")
        hass.states.async_set("sensor.prices", "1", {
            "prices": _stromligning_prices(now, intervals=4, cadence=quarter, default_price=1),
        })
        forecast = _stromligning_prices(now + hour, intervals=5, cadence=hour, default_price=2)
        forecast.pop(2)
        for row in forecast:
            row["end"] = (datetime.fromisoformat(row["start"]) + quarter).isoformat()
        hass.states.async_set(tomorrow, "on", {"forecast_data": True, "prices": forecast})

        slots = coordinator._price_slots(now)

        assert [slot.hours for slot in slots if slot.source == "known"] == [.25] * 4
        assert [slot.hours for slot in slots if slot.source == "forecast"] == [1, 1]
        assert slots[-1].end == now + 3 * hour
        assert coordinator._forecast_planning_source == tomorrow

    asyncio.run(scenario())


def test_stromligning_repair_preserves_gap_before_quarter_hour_data() -> None:
    now = datetime(2026, 11, 13, tzinfo=UTC)
    quarter = timedelta(minutes=15)
    starts = [now + timedelta(minutes=minute) for minute in (0, 60, 75, 90, 105)]
    rows = [
        {"start": start, "end": start + quarter, "price": 2}
        for start in starts
    ]
    assessment = assess_external_forecast(
        rows, now=now, reported_at=now, maximum_age=timedelta(hours=3),
        source_platform="stromligning",
    )
    assert all(slot.hours == .25 for slot in assessment.slots)


def test_coordinator_passes_current_and_historical_load_into_price_slots() -> None:
    """Live usage anchors nearby slots; matching historical usage informs later ones."""

    async def scenario() -> None:
        now = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
        rows = _stromligning_prices(
            now, intervals=19, cadence=timedelta(hours=1), default_price=2.0
        )
        hass = HomeAssistant("/tmp")
        coordinator = Fbp1200Coordinator(
            hass,
            _Entry(
                {
                    CONF_PRICE_ENTITIES: ["sensor.known"],
                    CONF_LOAD_POWER: "sensor.load",
                }
            ),
        )
        hass.states.async_set("sensor.known", "2.0", {"prices": rows})
        hass.states.async_set("sensor.load", "100")

        learner = coordinator.runtime.load_learner
        for offset_hours in (1, 18):
            target = now + timedelta(hours=offset_hours)
            for week in range(1, 13):
                for quarter in range(4):
                    learner.observe(
                        target - timedelta(weeks=week) + timedelta(minutes=quarter * 15),
                        500,
                    )
        learner.recent_w.clear()
        learner.recent_w.extend([100] * 8)

        slots = coordinator._price_slots(now)

        assert slots[1].expected_load_wh == pytest.approx(140)
        assert slots[18].expected_load_wh == pytest.approx(380)
        assert slots[18].expected_load_wh > slots[1].expected_load_wh

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

    merged = extend_known_horizon(
        known,
        forecast,
        now=known[-1].end,
        uncertainty_dkk_per_kwh=0.25,
    )

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

    merged = extend_known_horizon(
        known,
        forecast,
        now=known[-1].end,
        uncertainty_dkk_per_kwh=0.25,
    )

    assert [slot.hours for slot in merged] == [1.0, 0.25, 0.25, 0.25, 0.25]


def test_external_forecast_is_capped_at_three_days_and_stays_uncertain() -> None:
    """The forecast horizon is capped from now without relabeling forecast rows."""
    known = [_slot(0, 1.0)]
    now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    forecast_start = known[-1].end
    forecast = [
        PriceSlot(
            forecast_start + timedelta(hours=index),
            forecast_start + timedelta(hours=index + 1),
            1.0 + index,
        )
        for index in range(100)
    ]

    merged = extend_known_horizon(
        known,
        forecast,
        now=now,
        uncertainty_dkk_per_kwh=0.25,
    )

    assert merged[-1].end == now + timedelta(days=3)
    assert all(slot.source == "forecast" for slot in merged[1:])
    assert all(
        slot.uncertainty_dkk_per_kwh == 0.25 for slot in merged[1:]
    )
    assert merged[-1].hours == pytest.approx(0.5)


def test_forecast_is_not_appended_when_known_prices_reach_past_the_cap() -> None:
    """The cap applies after now even when the confirmed horizon is longer."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    known = [
        PriceSlot(
            now + timedelta(hours=index),
            now + timedelta(hours=index + 1),
            1.0,
        )
        for index in range(80)
    ]
    forecast = [
        PriceSlot(known[-1].end, known[-1].end + timedelta(hours=1), 2.0)
    ]

    merged = extend_known_horizon(
        known,
        forecast,
        now=now,
        uncertainty_dkk_per_kwh=0.25,
    )

    assert merged == known


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


def test_scored_forecast_is_not_recorded_again_after_refresh_or_restart() -> None:
    """A published actual price cannot turn revisions into new scored samples."""
    accuracy = ForecastAccuracy()
    accuracy.record_forecasts([_slot(1, 1.20)])
    assert accuracy.score_actual_prices(
        [_slot(1, 1.00)], uncertainty_dkk_per_kwh=0.25
    ) == 1

    for _ in range(2):
        accuracy.record_forecasts([_slot(1, 1.00)])
        assert accuracy.score_actual_prices(
            [_slot(1, 1.00)], uncertainty_dkk_per_kwh=0.25
        ) == 0
        assert accuracy.samples == 1
        assert accuracy.mean_absolute_error_dkk_per_kwh == 0.20
        accuracy = ForecastAccuracy.from_dict(accuracy.as_dict())


def test_scored_forecast_identities_are_bounded_and_expire() -> None:
    accuracy = ForecastAccuracy()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(1001):
        end = start + timedelta(minutes=index + 1)
        interval = PriceSlot(end - timedelta(minutes=1), end, 1.20)
        accuracy.record_forecasts([interval])
        accuracy.score_actual_prices([interval], uncertainty_dkk_per_kwh=0.25)
    restored = ForecastAccuracy.from_dict(accuracy.as_dict())

    assert restored.samples == 1000
    assert len(restored.as_dict()["scored_intervals"]) == 1000
    restored.prune(end + timedelta(days=8))
    assert restored.as_dict()["scored_intervals"] == {}
    assert restored.samples == 1000


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


def test_first_contiguous_forecast_extends_plan_and_all_sources_score_independently() -> (
    None
):
    """Configured source order selects planning; all sources retain evidence."""

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

        assert [slot.price for slot in slots] == [1.0, 1.20]
        assert coordinator._forecast_planning_source == "sensor.forecast_a"
        assert coordinator._forecast_used_slot_count == 1
        assert coordinator._forecast_sources["sensor.forecast_a"]["status"] == "used"
        assert (
            coordinator._forecast_sources["sensor.forecast_b"]["status"] == "available"
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


_ADAPT_NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def _adaptive_accuracy(
    error: float, *, quarter_hour: bool = False, days: int = 3,
    hours_per_day: int = 8, age_days: int = 3,
) -> ForecastAccuracy:
    accuracy = ForecastAccuracy()
    cadence = timedelta(minutes=15) if quarter_hour else timedelta(hours=1)
    predicted, actual = [], []
    for day in range(days):
        base = _ADAPT_NOW - timedelta(days=age_days - day)
        for index in range(hours_per_day * (4 if quarter_hour else 1)):
            start = base + cadence * index
            predicted.append(PriceSlot(start, start + cadence, 1 + error))
            actual.append(PriceSlot(start, start + cadence, 1))
    accuracy.record_forecasts(predicted, now=predicted[0].start - timedelta(hours=1))
    accuracy.score_actual_prices(actual, uncertainty_dkk_per_kwh=.35, now=_ADAPT_NOW)
    return accuracy


def _adapt(accuracies: dict[str, ForecastAccuracy], *, enabled: bool = True,
           floor: float = .35, now: datetime = _ADAPT_NOW) -> dict:
    return forecast_adaptation(
        list(accuracies), accuracies, now=now,
        configured_uncertainty=floor, enabled=enabled,
    )


def test_forecast_allowance_learns_nearer_vintages_without_rewriting_first_score() -> None:
    accuracy = ForecastAccuracy()
    for days_ago in (3, 2, 1):
        base = _ADAPT_NOW - timedelta(days=days_ago)
        actual = [PriceSlot(base + timedelta(hours=i), base + timedelta(hours=i + 1), 2)
                  for i in range(8)]
        for lead, price in ((60, .4), (40.9, 2.25), (39, 3)):
            accuracy.record_forecasts(
                [PriceSlot(slot.start, slot.end, price) for slot in actual],
                now=base - timedelta(hours=lead),
            )
        accuracy = ForecastAccuracy.from_dict(accuracy.as_dict())
        quarters = [
            PriceSlot(slot.start + timedelta(minutes=i), slot.start + timedelta(minutes=i + 15), 2)
            for slot in actual for i in range(0, 60, 15)
        ]
        assert accuracy.score_actual_prices(
            quarters, uncertainty_dkk_per_kwh=.2, now=base + timedelta(hours=8),
        ) == 8

    accuracy = ForecastAccuracy.from_dict(accuracy.as_dict())
    assert accuracy.samples == 24
    assert accuracy.mean_absolute_error_dkk_per_kwh == 1.6
    source = _adapt({"source": accuracy}, floor=.2)["sources"]["source"]
    assert source["effective_uncertainty_dkk_per_kwh"] == pytest.approx(1.6)
    assert source["lead_bands"]["24_48h"]["covered_hours"] == 24
    assert source["lead_bands"]["24_48h"]["effective_uncertainty_dkk_per_kwh"] == .25
    assert source["lead_bands"]["48_72h"]["effective_uncertainty_dkk_per_kwh"] == pytest.approx(1.6)
    assert source["lead_bands"]["0_24h"]["effective_uncertainty_dkk_per_kwh"] == pytest.approx(1.6)


def test_unchanged_forecast_does_not_become_a_new_vintage_on_poll() -> None:
    target = _slot(1, 2)
    accuracy = ForecastAccuracy()
    published = target.start - timedelta(hours=49)
    republished = target.start - timedelta(hours=47)
    accuracy.record_forecasts([target], now=published, publication_time=published)
    accuracy.record_forecasts([target], now=republished, publication_time=published)
    assert set(accuracy.lead_outstanding) == {"48_72h"}

    accuracy.record_forecasts([target], now=republished, publication_time=republished)
    assert set(accuracy.lead_outstanding) == {"24_48h", "48_72h"}


def test_forecast_adaptation_raises_uncertainty_and_prefers_common_period_accuracy() -> None:
    poor, good = _adaptive_accuracy(.6), _adaptive_accuracy(.1, quarter_hour=True)
    result = _adapt({"poor": poor, "good": good})
    assert result["source_order"] == ["good", "poor"]
    assert result["sources"]["poor"]["adaptive_ready"]
    assert result["sources"]["poor"]["covered_hours"] == 24
    assert result["sources"]["poor"]["effective_uncertainty_dkk_per_kwh"] == pytest.approx(.6)
    assert result["sources"]["good"]["effective_uncertainty_dkk_per_kwh"] == .35


@pytest.mark.parametrize("quarter_hour", [False, True])
def test_adaptive_confidence_counts_actual_hours_not_forecast_rows(quarter_hour: bool) -> None:
    accuracy = _adaptive_accuracy(.8, quarter_hour=quarter_hour, hours_per_day=2)
    source = _adapt({"source": accuracy})["sources"]["source"]
    assert source["covered_hours"] == 6
    assert not source["adaptive_ready"]
    assert source["effective_uncertainty_dkk_per_kwh"] == .35


def test_twenty_four_hours_from_one_day_are_not_enough_for_adaptation() -> None:
    accuracy = _adaptive_accuracy(.8, days=1, hours_per_day=24, age_days=1)
    source = _adapt({"source": accuracy})["sources"]["source"]
    assert source["covered_hours"] == 24
    assert not source["adaptive_ready"]


def test_legacy_aggregate_scores_remain_display_only_and_never_drive_adaptation() -> None:
    legacy = ForecastAccuracy.from_dict({
        "errors_dkk_per_kwh": [.9] * 1000, "within_uncertainty": [False] * 1000,
    })
    assert legacy.samples == 1000
    assert legacy.mean_absolute_error_dkk_per_kwh == .9
    source = _adapt({"legacy": legacy})["sources"]["legacy"]
    assert not source["adaptive_ready"]
    assert source["covered_hours"] == 0
    assert source["effective_uncertainty_dkk_per_kwh"] == .35


def test_stale_error_evidence_cannot_raise_uncertainty_or_change_priority() -> None:
    scores = {"poor": _adaptive_accuracy(.9), "good": _adaptive_accuracy(.1)}
    result = _adapt(scores, now=_ADAPT_NOW + timedelta(days=8))
    assert result["source_order"] == ["poor", "good"]
    assert all(not s["adaptive_ready"] for s in result["sources"].values())
    assert all(s["effective_uncertainty_dkk_per_kwh"] == .35 for s in result["sources"].values())


def test_forecast_sources_require_matching_historical_hours_before_reordering() -> None:
    scores = {"poor": _adaptive_accuracy(.9), "good": _adaptive_accuracy(.1, age_days=6)}
    result = _adapt(scores)
    assert all(s["adaptive_ready"] for s in result["sources"].values())
    assert result["source_order"] == ["poor", "good"]


@pytest.mark.parametrize("errors", [(0, 0), (.2, .19)])
def test_ties_and_improvements_below_ten_percent_keep_configured_order(errors: tuple) -> None:
    result = _adapt({"first": _adaptive_accuracy(errors[0]),
                     "second": _adaptive_accuracy(errors[1])})
    assert result["source_order"] == ["first", "second"]


def test_disabled_adaptation_preserves_user_floor_and_source_priority() -> None:
    scores = {"poor": _adaptive_accuracy(.9), "good": _adaptive_accuracy(.1)}
    result = _adapt(scores, enabled=False, floor=.7)
    assert result["source_order"] == ["poor", "good"]
    assert all(s["effective_uncertainty_dkk_per_kwh"] == .7 for s in result["sources"].values())
    assert all(not s["adaptive_ready"] for s in result["sources"].values())


def test_timestamped_forecast_errors_survive_restore_and_score_only_once() -> None:
    original = _adaptive_accuracy(.6)
    restored = ForecastAccuracy.from_dict(original.as_dict())
    assert restored.error_samples == original.error_samples
    assert _adapt({"source": restored})["sources"]["source"]["adaptive_ready"]
    first = restored.error_samples[0]
    interval = PriceSlot(datetime.fromisoformat(first["start"]),
                         datetime.fromisoformat(first["end"]), 1)
    restored.record_forecasts([interval])
    assert restored.score_actual_prices([interval], uncertainty_dkk_per_kwh=.35,
                                        now=_ADAPT_NOW) == 0
    assert len(restored.error_samples) == len(original.error_samples)


def test_malformed_timestamped_errors_are_not_trusted_or_serialized_as_nan() -> None:
    import json

    original = _adaptive_accuracy(.8).as_dict()
    original["error_samples"] += [None, {}, {"start": "invalid", "error_dkk_per_kwh": float("nan")}]
    original["outstanding"] = None
    original["scored_intervals"] = None
    original["errors_dkk_per_kwh"] = [float("nan"), "broken", .8]
    restored = ForecastAccuracy.from_dict(original)
    assert len(restored.error_samples) == 24
    assert _adapt({"source": restored})["sources"]["source"]["adaptive_ready"]
    json.dumps(restored.as_dict(), allow_nan=False)


def test_overlapping_error_rows_never_inflate_historical_coverage() -> None:
    accuracy = _adaptive_accuracy(.8, hours_per_day=2)
    accuracy.error_samples *= 20
    source = _adapt({"source": accuracy})["sources"]["source"]
    assert source["covered_hours"] == 6
    assert not source["adaptive_ready"]


def test_duration_weighted_p90_is_independent_of_error_row_cadence() -> None:
    accuracy = _adaptive_accuracy(.1)
    high_hours = accuracy.error_samples[:2]
    accuracy.error_samples = accuracy.error_samples[2:]
    for hour in high_hours:
        start = datetime.fromisoformat(hour["start"])
        for quarter in range(4):
            left = start + timedelta(minutes=15 * quarter)
            accuracy.error_samples.append({
                "start": left.isoformat(), "end": (left + timedelta(minutes=15)).isoformat(),
                "error_dkk_per_kwh": 1, "scored_at": _ADAPT_NOW.isoformat(),
                "issued_at": hour["issued_at"],
            })
    source = _adapt({"source": accuracy}, floor=0)["sources"]["source"]
    assert source["covered_hours"] == 24
    assert source["effective_uncertainty_dkk_per_kwh"] == pytest.approx(.1)


def test_hourly_forecast_errors_do_not_cancel_against_quarter_hour_actuals() -> None:
    accuracy = ForecastAccuracy()
    predicted, actual = [], []
    for day in range(3):
        base = _ADAPT_NOW - timedelta(days=3 - day)
        for hour in range(8):
            start = base + timedelta(hours=hour)
            predicted.append(PriceSlot(start, start + timedelta(hours=1), 1))
            for quarter in range(4):
                left = start + timedelta(minutes=15 * quarter)
                actual.append(PriceSlot(left, left + timedelta(minutes=15), 0 if quarter % 2 else 2))
    accuracy.record_forecasts(predicted, now=predicted[0].start - timedelta(hours=1))
    accuracy.score_actual_prices(actual, uncertainty_dkk_per_kwh=.35, now=_ADAPT_NOW)
    assert accuracy.mean_absolute_error_dkk_per_kwh == 0  # Backward-compatible display.
    source = _adapt({"source": accuracy})["sources"]["source"]
    assert source["adaptive_ready"]
    assert source["effective_uncertainty_dkk_per_kwh"] == 1


def test_legacy_pending_forecasts_cannot_become_fresh_adaptive_evidence() -> None:
    accuracy = ForecastAccuracy()
    predicted = _slot(1, 1.2)
    accuracy.record_forecasts([predicted])
    accuracy.score_actual_prices([_slot(1, 1)], uncertainty_dkk_per_kwh=.35,
                                 now=_ADAPT_NOW)
    assert accuracy.samples == 1
    assert not accuracy.error_samples


def test_future_published_actual_prices_do_not_yet_count_as_historical_coverage() -> None:
    accuracy = ForecastAccuracy()
    predicted = PriceSlot(_ADAPT_NOW + timedelta(hours=1),
                          _ADAPT_NOW + timedelta(hours=2), 1.8)
    actual = PriceSlot(predicted.start, predicted.end, 1)
    accuracy.record_forecasts([predicted], now=_ADAPT_NOW)
    accuracy.score_actual_prices([actual], uncertainty_dkk_per_kwh=.35, now=_ADAPT_NOW)
    source = _adapt({"source": accuracy})["sources"]["source"]
    assert len(accuracy.error_samples) == 1
    assert source["covered_hours"] == 0


def test_overlapping_or_forecast_actual_rows_never_score_a_prediction() -> None:
    accuracy = ForecastAccuracy()
    accuracy.record_forecasts([_slot(1, 1.2)], now=_slot(0, 1).start)
    assert accuracy.score_actual_prices([_slot(1, 1), _slot(1, 1)],
        uncertainty_dkk_per_kwh=.35, now=_ADAPT_NOW) == 0
    forecast_actual = PriceSlot(_slot(1, 1).start, _slot(1, 1).end, 1, source="forecast")
    assert accuracy.score_actual_prices([forecast_actual],
        uncertainty_dkk_per_kwh=.35, now=_ADAPT_NOW) == 0
    assert not accuracy.error_samples


def test_earliest_issued_forecast_wins_when_provider_changes_interval_cadence() -> None:
    accuracy = ForecastAccuracy()
    base = _ADAPT_NOW - timedelta(days=4)
    for day in range(3):
        start = base + timedelta(days=day)
        actual = [PriceSlot(start + timedelta(hours=hour),
                            start + timedelta(hours=hour + 1), 1) for hour in range(24)]
        accuracy.record_forecasts([PriceSlot(start, start + timedelta(hours=24), 5)],
                                  now=start - timedelta(days=1))
        accuracy.record_forecasts(actual, now=start - timedelta(hours=1))
        for interval in actual:
            accuracy.score_actual_prices([interval], uncertainty_dkk_per_kwh=.1,
                                         now=interval.end)
        # The earlier 24-hour prediction can only score after its entire
        # actual cover is available; its issue time must still take priority.
        accuracy.score_actual_prices(actual, uncertainty_dkk_per_kwh=.1,
                                     now=start + timedelta(days=1))
    restored = ForecastAccuracy.from_dict(accuracy.as_dict())
    source = _adapt({"source": restored}, floor=.1)["sources"]["source"]
    assert source["covered_hours"] == 72
    assert source["adaptive_ready"]
    assert source["effective_uncertainty_dkk_per_kwh"] == 4


def test_earlier_usable_prediction_blocks_only_its_overlapping_accuracy_evidence() -> None:
    accuracy = _adaptive_accuracy(0)
    first = datetime.fromisoformat(accuracy.error_samples[0]["start"])
    for day in range(3):
        start = first + timedelta(days=day)
        accuracy.record_forecasts([PriceSlot(start, start + timedelta(hours=24), 5)],
                                  now=first - timedelta(days=2))
    # Only the first earlier daily prediction starts inside its issue's 72 h
    # planning window. It blocks eight overlapping hours until complete cover
    # arrives; out-of-window predictions must not block the other sixteen.
    source = _adapt({"source": accuracy})["sources"]["source"]
    assert source["covered_hours"] == 16
    assert not source["adaptive_ready"]
