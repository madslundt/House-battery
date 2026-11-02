"""Weekly price patterns learn evidence rather than hard-coded calendar rules."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PriceSlot
from house_battery.price_patterns import PricePatternLearner

ZONE = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def _price(when: datetime, weekend_expensive: bool = False) -> float:
    local = when.astimezone(ZONE)
    weekend = local.weekday() >= 5
    base = 3 if weekend == weekend_expensive else 1
    return base + (2 if 17 <= local.hour < 21 else 0)


def _trained(*, weekend_expensive: bool = False, days: int = 42) -> PricePatternLearner:
    learner = PricePatternLearner()
    learner.configure("all-in-price-entity", "Europe/Copenhagen")
    slots = []
    for hour in range(days * 24):
        start = NOW - timedelta(days=days) + timedelta(hours=hour)
        slots.append(PriceSlot(start, start + timedelta(hours=1),
                               _price(start, weekend_expensive)))
    for day in range(days):
        end = NOW - timedelta(days=days - day - 1)
        assert learner.observe(slots[day * 24:(day + 1) * 24], end)
        learner.shadow_issue([PriceSlot(end, end + timedelta(hours=1),
                                       _price(end, weekend_expensive))], end)
    return learner


def test_learning_requires_four_independent_matching_weekly_dates() -> None:
    learner = _trained(days=20)
    prefix = [PriceSlot(NOW, NOW + timedelta(hours=1), 2)]
    assert learner.summary(NOW)["status"] == "learning"
    assert learner.extend(prefix, NOW, .3) == prefix


@pytest.mark.parametrize("weekend_expensive", [False, True])
def test_weekend_effect_is_learned_in_either_direction(weekend_expensive: bool) -> None:
    learner = _trained(weekend_expensive=weekend_expensive)
    summary = learner.summary(NOW)
    assert summary["status"] == "ready"
    assert summary["weekend_comparison_hours"] == 24
    assert summary["weekend_discount_dkk_per_kwh"] == pytest.approx(
        -2 if weekend_expensive else 2
    )
    assert summary["validation_samples"] > 0
    assert summary["validation_mae_dkk_per_kwh"] == 0


def test_forecast_extends_after_external_prefix_with_uncertainty_and_cap() -> None:
    learner = _trained()
    known = PriceSlot(NOW - timedelta(minutes=15), NOW + timedelta(hours=1), 8)
    external = PriceSlot(known.end, NOW + timedelta(hours=8, minutes=15), 9,
                         source="forecast", uncertainty_dkk_per_kwh=.4)
    result = learner.extend([known, external], NOW, .6)
    assert result[:2] == [known, external]
    assert result[2].start == external.end
    assert result[2].end == external.end.replace(minute=0) + timedelta(hours=1)
    assert result[-1].end == NOW + timedelta(hours=72)
    assert all(a.end == b.start for a, b in zip(result, result[1:]))
    assert all(slot.source == "forecast" and slot.uncertainty_dkk_per_kwh >= .6
               for slot in result[2:])
    for slot in result[2:]:
        assert slot.price == _price(slot.start)


def test_no_invented_current_tariff_or_interior_gap_repair() -> None:
    learner = _trained()
    known = PriceSlot(NOW, NOW + timedelta(hours=1), 2)
    later = PriceSlot(NOW + timedelta(hours=2), NOW + timedelta(hours=3), 2)
    assert learner.extend([], NOW, .1) == []
    assert learner.extend([later], NOW, .1) == [later]
    assert learner.extend([known, later], NOW, .1) == [known, later]
    forecast_only = PriceSlot(NOW, NOW + timedelta(hours=1), 1, source="forecast")
    assert learner.extend([forecast_only], NOW, .1) == [forecast_only]


def test_quarter_hours_are_duration_weighted_and_repeated_rows_do_not_train_twice() -> None:
    learner = PricePatternLearner()
    learner.configure("price", "Europe/Copenhagen")
    start = NOW - timedelta(hours=1)
    slots = [PriceSlot(start + timedelta(minutes=i * 15),
                       start + timedelta(minutes=(i + 1) * 15), price)
             for i, price in enumerate([1, 2, 3, 6])]
    for index, slot in enumerate(slots):
        learner.observe([slot], start + timedelta(minutes=(index + 1) * 15))
    assert learner.samples[start.isoformat()][0] == 3
    assert not learner.observe(slots * 20, NOW)
    assert len(learner.samples) == 1


def test_sparse_and_conflicting_hour_coverage_cannot_qualify() -> None:
    learner = PricePatternLearner()
    start = NOW - timedelta(hours=1)
    quarter = PriceSlot(start, start + timedelta(minutes=15), 1)
    learner.observe([quarter] * 10, NOW)
    assert not learner.samples
    conflicting = PriceSlot(start, NOW, 2)
    learner.observe([conflicting], NOW)
    assert not learner.samples


def test_future_prices_forecasts_and_negative_prices_are_handled_honestly() -> None:
    learner = PricePatternLearner()
    past = PriceSlot(NOW - timedelta(hours=1), NOW, -.5)
    future = PriceSlot(NOW, NOW + timedelta(hours=1), 100)
    forecast = PriceSlot(NOW - timedelta(hours=2), past.start, -100, source="forecast")
    learner.observe([past, future, forecast], NOW)
    assert learner.samples == {past.start.isoformat(): (-.5, None)}


def test_dst_repeated_hour_counts_as_one_independent_local_date() -> None:
    learner = _trained()
    # Copenhagen's 02:00 is repeated on 25 October 2026. The six-week sample
    # contains seven UTC rows at that bucket but exactly six local Sundays.
    values = learner._profile(NOW)[6, 2]
    assert len(values) == 6
    prefix = [PriceSlot(NOW, NOW + timedelta(hours=1), 2)]
    result = learner.extend(prefix, NOW, 0)
    assert all(slot.uncertainty_dkk_per_kwh >= .25 for slot in result[1:])


def test_stale_evidence_expires_and_source_or_timezone_changes_reset_learning() -> None:
    learner = _trained()
    later = NOW + timedelta(days=4)
    prefix = [PriceSlot(later, later + timedelta(hours=1), 2)]
    assert learner.summary(later)["status"] == "stale"
    assert learner.extend(prefix, later, .2) == prefix
    learner.configure("new-price", "Europe/Copenhagen")
    assert not learner.samples
    learner = _trained()
    learner.configure("all-in-price-entity", "UTC")
    assert not learner.samples
    learner = _trained()
    learner.prune(NOW + timedelta(days=91))
    assert not learner.samples


def test_storage_round_trip_and_malformed_evidence_are_safe() -> None:
    empty = PricePatternLearner()
    assert PricePatternLearner.from_dict(empty.as_dict()).as_dict() == empty.as_dict()
    for invalid in (None, [], "invalid", 4):
        assert PricePatternLearner.from_dict(invalid).as_dict() == empty.as_dict()
    learner = _trained()
    restored = PricePatternLearner.from_dict(learner.as_dict())
    assert restored.as_dict() == learner.as_dict()
    malformed = PricePatternLearner.from_dict({"samples": {
        "2026-10-26T10:00:00+00:00": [float("inf"), None],
        "2026-10-26T11:00:00": [1, None], "bad": [2, 3],
    }, "pending": {"bad": [[0, 3600, 1]]}})
    assert not malformed.samples and not malformed.pending


def test_chronological_error_scoring_cannot_train_on_later_weeks() -> None:
    learner = PricePatternLearner()
    learner.configure("price", "UTC")
    slots = [PriceSlot(NOW - timedelta(weeks=week, hours=1),
                       NOW - timedelta(weeks=week), float(week)) for week in range(6)]
    learner.observe(slots, NOW)
    errors = [error for _, error in learner.samples.values()]
    assert errors[:4] == [None] * 4
    assert errors[4] == pytest.approx(2.5)
    assert errors[5] == pytest.approx(3)


def test_one_observed_spike_does_not_poison_all_future_hour_uncertainty() -> None:
    learner = _trained()
    prefix = [PriceSlot(NOW, NOW + timedelta(hours=1), _price(NOW))]
    baseline = learner.extend(prefix, NOW, 0)[1].uncertainty_dkk_per_kwh
    recent = PriceSlot(NOW - timedelta(hours=1), NOW, 20)
    # A new actual observation in a later hour can score the prior model even
    # when the same weekday profile has yet to absorb the price regime shift.
    later = NOW + timedelta(hours=1)
    learner.observe([PriceSlot(NOW, later, recent.price)], later)
    future_prefix = [PriceSlot(later, later + timedelta(hours=1), _price(later))]
    raised = learner.extend(future_prefix, later, 0)[1].uncertainty_dkk_per_kwh
    assert raised == baseline == .25


def test_uncertainty_and_timestamp_inputs_are_validated() -> None:
    learner = PricePatternLearner()
    with pytest.raises(ValueError):
        learner.observe([], NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        learner.extend([], NOW, float("nan"))


def test_learned_negative_forecasts_remain_negative_without_losing_uncertainty() -> None:
    learner = _trained()
    learner.samples = {key: (price - 10, error)
                       for key, (price, error) in learner.samples.items()}
    prefix = [PriceSlot(NOW, NOW + timedelta(hours=1), -.5)]
    result = learner.extend(prefix, NOW, .5)
    assert len(result) > 1
    assert all(slot.price < 0 and slot.uncertainty_dkk_per_kwh >= .5
               for slot in result[1:])


@pytest.mark.parametrize("shifted_source", ["known", "forecast"])
def test_authoritative_future_regime_shift_widens_only_learned_uncertainty(
    shifted_source: str,
) -> None:
    learner = _trained()
    # Keep a constant learned level so the new five-DKK regime has an exact,
    # transparent four-DKK mismatch, independent of calendar profile shape.
    learner.samples = {key: (1, 0 if error is not None else None)
                       for key, (_, error) in learner.samples.items()}
    current = PriceSlot(NOW, NOW + timedelta(hours=1), 1)
    shifted = PriceSlot(current.end, current.end + timedelta(hours=2), 5,
                         source=shifted_source, uncertainty_dkk_per_kwh=.3)
    result = learner.extend([current, shifted], NOW, .1)
    assert result[:2] == [current, shifted]
    assert all(slot.price == 1 and slot.uncertainty_dkk_per_kwh >= 4
               for slot in result[2:])


def test_matching_calendar_pattern_does_not_create_a_regime_shift_penalty() -> None:
    learner = _trained()
    prefix = [PriceSlot(NOW + timedelta(hours=hour),
                         NOW + timedelta(hours=hour + 1),
                         _price(NOW + timedelta(hours=hour))) for hour in range(12)]
    result = learner.extend(prefix, NOW, .1)
    assert result[:12] == prefix
    assert all(slot.uncertainty_dkk_per_kwh == pytest.approx(.25)
               for slot in result[12:])
