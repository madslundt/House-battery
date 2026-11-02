"""Price-model activation requires honest issued skill, not a calendar sample count."""

import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.models import PriceSlot
from house_battery.pattern_models import LEAD_BANDS, PatternEvidence
from house_battery.price_patterns import PricePatternLearner

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)
ZONE = ZoneInfo("Europe/Copenhagen")


def shaped_price(when: datetime) -> float:
    local = when.astimezone(ZONE)
    return (1 if local.weekday() >= 5 else 3) + (2 if 17 <= local.hour < 21 else 0)


def collect_causal_history(
    learner: PricePatternLearner, now: datetime, price_for: Callable[[datetime], float] = shaped_price,
    *, days: int = 42, prefix_hours: int = 1,
) -> PricePatternLearner:
    """Advance actual time and issue predictions before their target prices arrive."""
    beginning = now - timedelta(days=days)
    for day in range(days):
        end = beginning + timedelta(days=day + 1)
        past = [PriceSlot(end - HOUR * (24 - hour), end - HOUR * (23 - hour),
                          price_for(end - HOUR * (24 - hour))) for hour in range(24)]
        learner.observe(past, end)
        learner.shadow_issue([PriceSlot(end + HOUR * hour, end + HOUR * (hour + 1),
                                       price_for(end + HOUR * hour)) for hour in range(prefix_hours)], end)
    return learner


def trained(**kwargs) -> PricePatternLearner:
    learner = PricePatternLearner()
    learner.configure("test-import", ZONE.key)
    return collect_causal_history(learner, NOW, **kwargs)


def test_retrospective_calendar_coverage_alone_never_authorizes_learned_control():
    learner = PricePatternLearner()
    learner.configure("price", ZONE.key)
    history = [PriceSlot(NOW - HOUR * offset, NOW - HOUR * (offset - 1),
                         shaped_price(NOW - HOUR * offset)) for offset in range(1008, 0, -1)]
    learner.observe(history, NOW)
    prefix = [PriceSlot(NOW, NOW + HOUR, shaped_price(NOW))]
    before = learner.summary(NOW)
    assert before["history_ready"] is True and before["status"] == "learning"
    assert learner.extend(prefix, NOW, .25) == prefix
    assert learner.evidence.issued  # Shadow evidence is collected without dispatch permission.


def test_issued_skill_activates_on_identical_targets_with_independent_dates():
    learner = trained()
    summary = learner.summary(NOW)
    assert summary["status"] == "ready"
    for band in LEAD_BANDS:
        evidence = summary["model_selection"][band]
        assert evidence["qualified"] and evidence["hours"] >= 24 and evidence["local_dates"] >= 3
        chosen = summary["selected_models"][band]
        assert chosen and evidence["mae_by_model"][chosen] == 0
        assert evidence["mae_by_model"]["flat_7d"] > .25
    prefix = [PriceSlot(NOW, NOW + HOUR, shaped_price(NOW))]
    result = learner.extend(prefix, NOW, .25)
    assert len(result) > 1
    assert all(slot.price == shaped_price(slot.start) for slot in result[1:])


def test_a_flat_zero_error_tie_collects_evidence_but_does_not_claim_pattern_skill():
    learner = trained(price_for=lambda _: 3)
    prefix = [PriceSlot(NOW, NOW + HOUR, 3)]
    assert learner.summary(NOW)["issued_validation_samples"] > 24
    assert learner.summary(NOW)["history_ready"]
    assert not any(learner.summary(NOW)["selected_models"].values())
    assert learner.extend(prefix, NOW, .25) == prefix


def test_repeated_polls_and_restored_duplicate_scores_cannot_manufacture_confidence():
    learner = trained()
    prefix = [PriceSlot(NOW, NOW + HOUR, shaped_price(NOW))]
    learner.shadow_issue(prefix, NOW + timedelta(minutes=1))
    before = len(learner.evidence.issued)
    for minute in range(2, 15):
        learner.shadow_issue(prefix, NOW + timedelta(minutes=minute))
    assert len(learner.evidence.issued) == before
    data = learner.as_dict()
    data["model_evidence"]["scores"] *= 5
    restored = PricePatternLearner.from_dict(data)
    assert len(restored.evidence.scores) == len({(row["start"], row["lead_band"])
                                               for row in learner.evidence.scores})
    assert restored.summary(NOW)["model_selection"] == learner.summary(NOW)["model_selection"]


def test_restored_future_or_incomplete_hours_never_qualify_current_comparisons():
    learner = trained()
    data = deepcopy(learner.as_dict())
    sample = data["model_evidence"]["scores"][0]
    sample["start"] = (NOW - timedelta(minutes=30)).isoformat()
    restored = PricePatternLearner.from_dict(data)
    assert not any(row["start"] == sample["start"] for row in restored.evidence.scores)
    evidence = PatternEvidence.from_dict({"champions": [], "issued": None, "scores": "bad"})
    assert not evidence.issued and not evidence.scores and not evidence.champions


def test_exported_forecast_evidence_is_detached_from_live_model_qualification():
    learner = trained()
    learner.summary(NOW)  # Materialize the qualified control-model diagnostics.
    before = learner.as_dict()
    exported = learner.as_dict()
    evidence = exported["model_evidence"]
    next(iter(evidence["issued"].values()))["predictions"]["day_type_28d"] = 999
    evidence["scores"][0]["errors"]["day_type_28d"] = 999
    evidence["champions"]["24_48h"] = "flat_7d"
    evidence["scores"].clear()
    assert learner.as_dict() == before


def test_transition_can_withhold_control_without_promoting_an_unvalidated_shadow_candidate():
    changed = NOW - timedelta(days=28)
    learner = trained(days=70, price_for=lambda when: shaped_price(when) + (3 if when >= changed else 0))
    summary = learner.summary(NOW)
    assert not any(summary["selected_models"].values())
    evidence = summary["model_selection"]["24_48h"]
    assert evidence["qualified"]
    assert evidence["mae_by_model"]["day_type_14d"] < evidence["mae_by_model"]["day_type_28d"]
    prefix = [PriceSlot(NOW, NOW + HOUR, shaped_price(NOW) + 3)]
    assert learner.extend(prefix, NOW, .25) == prefix


def test_a_settled_regime_can_requalify_the_fixed_control_model():
    changed = NOW - timedelta(days=56)
    learner = trained(days=98, price_for=lambda when: shaped_price(when) + (3 if when >= changed else 0))
    summary = learner.summary(NOW)
    assert all(model == "day_type_28d" for model in summary["selected_models"].values())
    prefix = [PriceSlot(NOW, NOW + HOUR, shaped_price(NOW) + 3)]
    result = learner.extend(prefix, NOW, .25)
    assert len(result) > 1
    assert all(slot.price == shaped_price(slot.start) + 3 for slot in result[1:])


def test_source_and_timezone_changes_clear_issued_forecasts_and_selection_as_well_as_history():
    learner = trained()
    assert learner.evidence.issued and learner.evidence.scores
    learner.configure("different-price-basis", ZONE.key)
    assert not learner.samples and not learner.evidence.issued and not learner.evidence.scores
    restored = PricePatternLearner.from_dict(trained().as_dict())
    assert restored.evidence.as_dict() == trained().evidence.as_dict()
    restored.configure("test-import", "UTC")
    assert not restored.evidence.issued and not restored.evidence.champions


def test_one_published_outlier_does_not_set_every_future_hours_allowance():
    learner = trained()
    prefix = [PriceSlot(NOW + HOUR * hour, NOW + HOUR * (hour + 1),
                        shaped_price(NOW + HOUR * hour) + (20 if hour == 5 else 0))
              for hour in range(24)]
    result = learner.extend(prefix, NOW, .25)
    assert len(result) > len(prefix)
    assert all(slot.uncertainty_dkk_per_kwh == .25 for slot in result[len(prefix):])


@pytest.mark.parametrize("end", [datetime(2026, 4, 6, 12, tzinfo=UTC), NOW])
def test_local_offset_published_rows_score_without_restart_across_dst(end):
    learner = PricePatternLearner()
    learner.configure("copenhagen-provider", ZONE.key)
    beginning = end - timedelta(days=42)
    for day in range(42):
        now = beginning + timedelta(days=day + 1)
        learner.observe([PriceSlot(now - HOUR * offset, now - HOUR * (offset - 1),
                                   shaped_price(now - HOUR * offset))
                         for offset in range(24, 0, -1)], now)
        # Provider JSON parses to explicit +01/+02 offsets. Store immutable UTC
        # identities even though its prefix keeps the local display offset.
        local_start = datetime.fromisoformat(now.astimezone(ZONE).isoformat())
        local_end = datetime.fromisoformat((now + HOUR).astimezone(ZONE).isoformat())
        learner.shadow_issue([PriceSlot(local_start, local_end, shaped_price(now))], now)
    summary = learner.summary(end)
    assert summary["status"] == "ready" and summary["issued_validation_samples"] > 24
    assert all(row["start"].endswith("+00:00") for row in learner.evidence.scores)
    assert all(model is not None for model in summary["selected_models"].values())


def test_stable_cap_does_not_backdate_actual_issuance_or_move_the_horizon():
    learner = trained()
    anchor = NOW.replace(minute=0)
    now = anchor + timedelta(minutes=7)
    cap = anchor + timedelta(hours=72)
    prefix = [PriceSlot(anchor, anchor + HOUR, shaped_price(anchor))]
    learner.evidence.issued.clear()
    result = learner.extend(prefix, now, .25, horizon_end=cap)
    assert result[-1].end == cap
    assert learner.evidence.issued
    assert all(datetime.fromisoformat(row["issued_at"]) == now for row in learner.evidence.issued.values())
    later = now + timedelta(minutes=1)
    repeated = learner.extend(prefix, later, .25, horizon_end=cap)
    assert repeated[-1].end == cap
    assert all(datetime.fromisoformat(row["issued_at"]) >= now for row in learner.evidence.issued.values())


@pytest.mark.parametrize(("month", "day", "quarters"), [(3, 31, 92), (10, 27, 100)])
def test_independent_hourly_dataset_retains_dst_duration_and_declares_resolution(month, day, quarters):
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    from compare_pattern_models import normalize_prices

    start = datetime(2024, month, day, tzinfo=ZONE)
    end = (start + timedelta(days=1)).astimezone(UTC)
    cursor = start.astimezone(UTC)
    records = []
    while cursor < end:
        records.append({"HourUTC": cursor.replace(tzinfo=None).isoformat(),
                        "SpotPriceDKK": 1250, "PriceArea": "DK2"})
        cursor += HOUR
    prices, resolution = normalize_prices({"records": records})
    assert len(prices) == quarters and all(slot.price == 1.25 for slot in prices)
    assert resolution == "native_hourly_repeated_for_common_scoring_no_intrahour_information"
