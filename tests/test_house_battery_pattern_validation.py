"""The research replay preserves elapsed-data boundaries and matched scoring."""

import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import validate_price_patterns as replay


def _payload(start: datetime, end: datetime) -> dict:
    records = []
    cursor = start.astimezone(UTC)
    while cursor < end.astimezone(UTC):
        local = cursor.astimezone(replay.ZONE)
        price = (1.0 if local.weekday() < 5 else .4) + (1.5 if 17 <= local.hour < 21 else 0)
        records.append({"TimeUTC": cursor.replace(tzinfo=None).isoformat(),
                        "PriceArea": "DK2", "DayAheadPriceDKK": price * 1000})
        cursor += replay.QUARTER
    return {"records": records}


@pytest.mark.parametrize(("month", "day", "quarters"), [(3, 29, 92), (10, 25, 100)])
def test_official_data_replay_preserves_actual_dst_day_duration(month, day, quarters):
    start = datetime(2026, month, day, tzinfo=replay.ZONE)
    result = replay.load_prices(_payload(start, start + timedelta(days=1)))
    assert len(result) == quarters
    assert all(b.start - a.start == replay.QUARTER for a, b in zip(result, result[1:]))


def test_official_data_replay_rejects_gaps_duplicates_and_partial_days():
    start = datetime(2026, 1, 1, tzinfo=replay.ZONE)
    payload = _payload(start, start + timedelta(days=1))
    for records in (payload["records"][1:],
                    payload["records"] + [payload["records"][0]],
                    payload["records"][:20] + payload["records"][21:]):
        with pytest.raises(ValueError):
            replay.load_prices({"records": records})


def test_walk_forward_scores_actual_model_and_never_trains_future_prices(monkeypatch):
    observed_calls = []
    original = replay.PricePatternLearner

    class AuditedLearner(original):
        def observe(self, slots, now):
            assert all(slot.end <= now for slot in slots)
            observed_calls.append(now)
            return super().observe(slots, now)

        def extend(self, slots, now, uncertainty_floor):
            assert all(datetime.fromisoformat(stamp) + replay.HOUR <= now
                       for stamp in self.samples)
            assert slots[0].start == now
            return super().extend(slots, now, uncertainty_floor)

    monkeypatch.setattr(replay, "PricePatternLearner", AuditedLearner)
    start = datetime(2026, 1, 1, tzinfo=replay.ZONE)
    prices = replay.load_prices(_payload(start, start + timedelta(days=42)))
    result = replay.validate(prices, warmup_days=28)

    assert len(observed_calls) == 41
    assert result["first_fully_ready_issue"] == (
        datetime.combine(start.date() + timedelta(days=32), time(), replay.ZONE)
        .astimezone(UTC).isoformat()
    )
    assert 0 < result["matched_quarter_instances"] < result["eligible_quarter_instances"]
    actual = result["overall"][replay.MODELS[0]]
    assert actual["mae"] == pytest.approx(0)
    assert actual["within_model_uncertainty_pct"] == 100
    assert result["overall"]["flat_previous_7d_mean"]["mae"] > .2
    assert all(model["forecast_instances"] == result["matched_quarter_instances"]
               for model in result["overall"].values())
