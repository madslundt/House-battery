"""Tests for local adaptive learning and normalized price data."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from fossibot_fbp1200.learning import BatteryLearner, LoadLearner
from fossibot_fbp1200.price import normalize_price_rows


def test_load_profile_ramps_historical_weight_after_four_observations() -> None:
    learner = LoadLearner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    learner.recent_w.extend([100, 100, 100, 100])
    for week in range(4):
        learner.observe(when - timedelta(weeks=week), 500)
    assert 100 < learner.predict_w(when) < 500


def test_load_model_round_trips_through_persistence() -> None:
    learner = LoadLearner()
    when = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    learner.observe(when, 420)
    restored = LoadLearner.from_dict(learner.as_dict())
    assert restored.as_dict() == learner.as_dict()


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


def test_hourly_price_rows_are_split_for_quarter_hour_load_learning() -> None:
    result = normalize_price_rows(
        [
            {
                "start": "2026-09-20T10:00:00+02:00",
                "end": "2026-09-20T11:00:00+02:00",
                "price": 1.2,
            }
        ]
    )
    assert len(result) == 4
    assert all(slot.end - slot.start == timedelta(minutes=15) for slot in result)
