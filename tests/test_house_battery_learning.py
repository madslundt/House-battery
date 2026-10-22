"""Tests for local adaptive learning and normalized price data."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.learning import BatteryLearner, LoadLearner
from house_battery.price import normalize_price_rows


def test_load_profile_ramps_historical_weight_after_four_observations() -> None:
    learner = LoadLearner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    learner.recent_w.extend([100, 100, 100, 100])
    for week in range(4):
        learner.observe(when - timedelta(weeks=week), 500)
    assert 100 < learner.predict_w(when) < 500


def test_load_prediction_blends_live_and_historical_usage_by_horizon() -> None:
    learner = LoadLearner()
    now = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    for offset_hours in (1, 18):
        for week in range(12):
            learner.observe(now + timedelta(hours=offset_hours, weeks=-week), 500)
    learner.recent_w.clear()
    learner.recent_w.extend([100] * 8)

    near = learner.predict_w(now + timedelta(hours=1), now=now)
    far = learner.predict_w(now + timedelta(hours=18), now=now)

    assert near == pytest.approx(140)
    assert far == pytest.approx(380)
    assert far > near


def test_load_model_round_trips_through_persistence() -> None:
    learner = LoadLearner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    learner.observe(when, 420)
    restored = LoadLearner.from_dict(learner.as_dict())
    assert restored.as_dict() == learner.as_dict()


def test_load_learning_expires_stale_inputs_without_erasing_recent_habits() -> None:
    learner = LoadLearner()
    now = datetime(2026, 10, 22, 10, tzinfo=UTC)
    learner.observe(now - timedelta(days=91), 900)
    stale_key = learner.key(now - timedelta(days=91))
    learner.observe(now - timedelta(days=1), 400)
    recent_key = learner.key(now - timedelta(days=1))

    learner.prune(now)

    assert stale_key not in learner.buckets
    assert learner.buckets[recent_key].mean_w == 400
    assert not learner.recent_w
    assert learner.predict_w(now, fallback_w=150) == 150


def test_load_profile_adapts_after_a_long_established_consumption_pattern() -> None:
    learner = LoadLearner()
    now = datetime(2026, 10, 22, 10, tzinfo=UTC)
    for _ in range(10000):
        learner.observe(now, 100)
    for _ in range(12):
        learner.observe(now, 1000)

    bucket = learner.buckets[learner.key(now)]
    assert bucket.count == 12
    assert bucket.mean_w > 650


def test_legacy_profile_is_preserved_on_migration_then_expires() -> None:
    learner = LoadLearner.from_dict(
        {
            "buckets": {"0:40": {"count": 10000, "mean_w": 400, "m2": 100}},
            "recent_w": [999],
        }
    )
    now = datetime(2026, 10, 22, 10, tzinfo=UTC)

    learner.prune(now)
    assert learner.buckets["0:40"].mean_w == 400
    assert learner.buckets["0:40"].count == 12
    assert not learner.recent_w

    learner.prune(now + timedelta(days=91))
    assert not learner.buckets


def test_load_profile_uses_configured_local_time_across_dst_offsets() -> None:
    """The same Copenhagen wall-clock time must share one learning bucket."""
    learner = LoadLearner(time_zone=ZoneInfo("Europe/Copenhagen"))
    winter_utc = datetime(2026, 1, 5, 17, 0, tzinfo=UTC)
    summer_utc = datetime(2026, 7, 6, 16, 0, tzinfo=UTC)

    for week in range(4):
        learner.observe(summer_utc - timedelta(weeks=week), 410)
    learner.recent_w.clear()
    learner.recent_w.extend([100] * 8)

    assert learner.key(winter_utc) == learner.key(summer_utc) == "0:72"
    assert learner.predict_w(summer_utc) > 100


def test_battery_learning_needs_five_stable_samples() -> None:
    learner = BatteryLearner()
    for value in [1900, 1920, 1910, 1930]:
        learner.add_capacity_sample(value)
    assert learner.learned_capacity_wh is None
    learner.add_capacity_sample(1915)
    assert learner.capacity_ready
    assert learner.learned_capacity_wh == 1915


def test_price_rows_require_timezone_and_are_deduplicated() -> None:
    rows = [
        {
            "start": "2026-09-20T10:00:00+02:00",
            "end": "2026-09-20T10:15:00+02:00",
            "price": 1.2,
        },
        {
            "start": "2026-09-20T10:00:00+02:00",
            "end": "2026-09-20T10:15:00+02:00",
            "price": 1.1,
        },
        {"start": "2026-09-20T10:15:00", "end": "2026-09-20T10:30:00", "price": 1.0},
    ]
    result = normalize_price_rows(rows)
    assert len(result) == 1
    assert result[0].price == 1.1


def test_hourly_price_rows_keep_the_provider_interval_duration() -> None:
    result = normalize_price_rows(
        [
            {
                "start": "2026-09-20T10:00:00+02:00",
                "end": "2026-09-20T11:00:00+02:00",
                "price": 1.2,
            }
        ]
    )
    assert len(result) == 1
    assert result[0].end - result[0].start == timedelta(hours=1)
