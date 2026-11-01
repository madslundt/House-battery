"""Issued load predictions are scored against covered future observations."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.load_forecast_evidence import LoadForecastEvidence

BASE = datetime(2026, 11, 1, tzinfo=UTC)


def prediction(start=BASE, wh=100):
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(),
            "baseline_wh": wh, "scheduled_wh": 0.0}


def test_first_issued_prediction_per_lead_band_survives_replanning_and_restart():
    evidence = LoadForecastEvidence()
    evidence.configure("load-a")
    evidence.record_forecasts(BASE - timedelta(hours=6), [prediction()], "v1")
    evidence.record_forecasts(BASE - timedelta(hours=5), [prediction(wh=999)], "v1")
    restored = LoadForecastEvidence.from_dict(evidence.as_dict())
    restored.observe_actual(BASE, BASE + timedelta(minutes=15), 125, 900)
    band = restored.summary(BASE + timedelta(minutes=15))["by_lead_band"]["2-6h"]
    assert band["samples"] == 1
    assert band["bias_wh"] == pytest.approx(25)
    assert band["mae_w"] == pytest.approx(100)


def test_sparse_actuals_never_score_forecasts():
    evidence = LoadForecastEvidence()
    evidence.record_forecasts(BASE - timedelta(hours=1), [prediction()], "v1")
    evidence.observe_actual(BASE, BASE + timedelta(minutes=15), 100, 60)
    assert evidence.summary(BASE + timedelta(minutes=15))["samples"] == 0


def test_actual_accumulator_splits_boundaries_and_does_not_bridge_restart():
    evidence = LoadForecastEvidence()
    for minute in range(16):
        rows = evidence.observe(BASE + timedelta(minutes=minute), 400, 2.0)
    assert len(rows) == 1
    assert rows[0]["load_wh"] == pytest.approx(100)
    assert rows[0]["covered_seconds"] == 900
    assert rows[0]["price"] == 2
    restored = LoadForecastEvidence.from_dict(evidence.as_dict())
    assert restored.observe(BASE + timedelta(minutes=30), 400, 2) == []
    assert restored.observe(BASE + timedelta(minutes=45), 400, 2) == []


def test_missing_price_keeps_load_evidence_but_does_not_invent_financial_actual():
    evidence = LoadForecastEvidence()
    for minute in range(16):
        rows = evidence.observe(BASE + timedelta(minutes=minute), 400, None)
    assert rows[0]["quality"] == "good"
    assert rows[0]["price"] is None
    assert rows[0]["load_wh"] == pytest.approx(100)


def test_negative_and_zero_known_prices_are_preserved():
    for price in (-0.5, 0.0):
        evidence = LoadForecastEvidence()
        for minute in range(16):
            rows = evidence.observe(BASE + timedelta(minutes=minute), 400, price)
        assert rows[0]["price"] == price


def test_load_source_change_discards_incompatible_forecasts_and_scores():
    evidence = LoadForecastEvidence()
    evidence.configure("load-a")
    evidence.record_forecasts(BASE - timedelta(hours=1), [prediction()], "v1")
    evidence.configure("load-b")
    evidence.observe_actual(BASE, BASE + timedelta(minutes=15), 125, 900)
    assert evidence.summary(BASE)["samples"] == 0


def test_corrupt_saved_evidence_is_rejected_and_old_forecasts_expire():
    evidence = LoadForecastEvidence.from_dict({"pending": [None, {"start": "bad"}],
                                               "scored": [None, {"error_wh": float("nan")}]})
    evidence.record_forecasts(BASE - timedelta(hours=1), [prediction()], "v1")
    evidence.prune(BASE + timedelta(days=10))
    assert evidence.summary(BASE + timedelta(days=10))["pending"] == 0
    assert evidence.summary(BASE + timedelta(days=10))["samples"] == 0


def test_partial_or_already_started_target_is_not_presented_as_issued_forecast():
    evidence = LoadForecastEvidence()
    evidence.record_forecasts(BASE + timedelta(minutes=1), [prediction()], "v1")
    assert evidence.summary(BASE)["pending"] == 0


def test_both_endpoints_must_be_valid_and_offline_gaps_have_no_coverage():
    evidence = LoadForecastEvidence()
    evidence.observe(BASE, 400, 2)
    evidence.observe(BASE + timedelta(minutes=1), None, 2)
    evidence.observe(BASE + timedelta(minutes=14), 400, 2)
    rows = evidence.observe(BASE + timedelta(minutes=15), 400, 2)
    assert rows[0]["covered_seconds"] == 60
    assert rows[0]["quality"] == "incomplete"


def test_mixed_price_quarter_scores_load_but_is_excluded_from_replay_settlement():
    evidence = LoadForecastEvidence()
    evidence.record_forecasts(BASE - timedelta(hours=1), [prediction()], "v1")
    for minute in range(16):
        rows = evidence.observe(BASE + timedelta(minutes=minute), 400, 0 if minute < 5 else 3)
    assert evidence.summary(BASE + timedelta(minutes=15))["samples"] == 1
    assert rows[0]["price"] is None
    assert rows[0]["price_quality"] == "mixed_price_interval"


def test_restored_daily_numeric_strings_are_normalized_before_scoring():
    evidence = LoadForecastEvidence()
    evidence.record_forecasts(BASE - timedelta(hours=1), [prediction()], "v1")
    data = evidence.as_dict()
    data["daily"] = [{"day": BASE.date().isoformat(), "lead_band": "0-2h",
                      "samples": "1", "absolute_error_wh": "0", "error_wh": "0",
                      "covered_seconds": "900"}]
    restored = LoadForecastEvidence.from_dict(data)
    restored.observe_actual(BASE, BASE + timedelta(minutes=15), 125, 900)
    assert restored.summary(BASE + timedelta(minutes=15))["daily"][0]["samples"] == 2
