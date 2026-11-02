"""External residual corrections require independent, genuinely held-out skill."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.forecast import ForecastAccuracy
from house_battery.forecast_calibration import calibrate_forecast
from house_battery.models import PriceSlot

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def history(errors: list[float], *, lead: int = 36, duplicate: int = 1) -> ForecastAccuracy:
    rows = []
    for index, error in enumerate(errors):
        start = NOW - HOUR * (len(errors) - index)
        row = {"start": start.isoformat(), "end": (start + HOUR).isoformat(),
               "issued_at": (start - HOUR * lead).isoformat(),
               "scored_at": (start + HOUR).isoformat(), "error_dkk_per_kwh": error}
        rows.extend([row] * duplicate)
    return ForecastAccuracy(error_samples=rows)


def forecast(*, lead: int = 36, price: float = 2, uncertainty: float = .35) -> PriceSlot:
    start = NOW + HOUR * lead
    return PriceSlot(start, start + HOUR, price, expected_load_wh=100,
                     source="forecast", uncertainty_dkk_per_kwh=uncertainty)


def test_consistent_bias_corrects_only_after_chronological_validation():
    slot = forecast()
    calibrated, diagnostic = calibrate_forecast(
        [slot], history([.5] * 168), now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert calibrated[0].price == pytest.approx(1.6)
    assert calibrated[0].expected_load_wh == 100
    assert calibrated[0].uncertainty_dkk_per_kwh >= .35
    assert diagnostic["status"] == "active"
    assert diagnostic["point_forecasts"][0]["calibrated_price_dkk_per_kwh"] == pytest.approx(1.6)
    band = diagnostic["lead_bands"]["24_48h"]
    assert band["validation_hours"] >= 24 and band["validation_days"] >= 2
    assert band["calibrated_mae_dkk_per_kwh"] < band["raw_mae_dkk_per_kwh"] * .9


def test_training_evidence_without_issued_out_of_sample_validation_is_neutral():
    slot = forecast()
    result, diagnostic = calibrate_forecast(
        [slot], history([.5] * 72, lead=72), now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result == [slot]
    assert diagnostic["status"] == "learning"


def test_disabled_and_known_prices_are_unchanged():
    known = PriceSlot(NOW, NOW + HOUR, 2)
    slot = forecast()
    evidence = history([.5] * 168)
    disabled, diagnostic = calibrate_forecast(
        [known, slot], evidence, now=NOW, enabled=False, uncertainty_floor=.25,
    )
    assert disabled == [known, slot] and diagnostic["status"] == "disabled"
    enabled, _ = calibrate_forecast(
        [known, slot], evidence, now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert enabled[0] is known
    assert enabled[1].price < slot.price


def test_unbiased_noise_and_harmful_large_errors_do_not_qualify():
    slot = forecast()
    for errors in ([.5, -.5] * 84, [.5] * 4 + [-.5]):
        errors = (errors * 168)[:168]
        result, diagnostic = calibrate_forecast(
            [slot], history(errors), now=NOW, enabled=True, uncertainty_floor=.25,
        )
        assert result == [slot]
        assert diagnostic["status"] != "active"


def test_recent_regime_reversal_cancels_previously_helpful_correction():
    slot = forecast()
    result, diagnostic = calibrate_forecast(
        [slot], history([.5] * 156 + [-.5] * 12), now=NOW,
        enabled=True, uncertainty_floor=.25,
    )
    assert result == [slot]
    assert diagnostic["lead_bands"]["24_48h"]["reason"] == "recent_regime_changed"


def test_duplicate_and_partial_hours_never_manufacture_independent_evidence():
    slot = forecast()
    result, diagnostic = calibrate_forecast(
        [slot], history([.5] * 12, duplicate=100), now=NOW,
        enabled=True, uncertainty_floor=.25,
    )
    assert result == [slot]
    assert diagnostic["lead_bands"]["24_48h"]["covered_hours"] == 12
    sparse = history([.5] * 168)
    for row in sparse.error_samples:
        row["end"] = (datetime.fromisoformat(row["start"]) + timedelta(minutes=15)).isoformat()
    result, diagnostic = calibrate_forecast(
        [slot], sparse, now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result == [slot]
    assert diagnostic["lead_bands"]["24_48h"]["covered_hours"] == 0


def test_future_scoring_and_stale_or_missing_history_are_ignored():
    slot = forecast()
    evidence = history([.5] * 168)
    for row in evidence.error_samples:
        row["scored_at"] = (NOW + HOUR).isoformat()
    for source in (evidence, ForecastAccuracy(), history([.5] * 168)):
        when = NOW + timedelta(days=8) if source is not evidence and source.error_samples else NOW
        result, diagnostic = calibrate_forecast(
            [slot], source, now=when, enabled=True, uncertainty_floor=.25,
        )
        assert result == [slot]
        assert diagnostic["status"] == "learning"


def test_calibration_is_source_specific_and_lead_specific():
    slot = forecast(lead=12)
    result, diagnostic = calibrate_forecast(
        [slot], history([.5] * 168, lead=36), now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result == [slot]
    assert diagnostic["lead_bands"]["0_24h"]["covered_hours"] == 0


def test_target_ordering_cannot_substitute_for_results_available_at_issue_time():
    evidence = history([.5] * 168)
    # All predictions were issued in one batch before any actual result was
    # available. A target-ordered replay would falsely call later targets held out.
    for row in evidence.error_samples:
        row["issued_at"] = (NOW - timedelta(days=7)).isoformat()
    result, diagnostic = calibrate_forecast(
        [forecast()], evidence, now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result == [forecast()]
    assert all(band["validation_hours"] == 0 for band in diagnostic["lead_bands"].values())


def test_conflicting_overlap_rejects_hour_instead_of_averaging_revisions():
    evidence = history([.5] * 168)
    evidence.error_samples += [dict(row, error_dkk_per_kwh=-.5)
                               for row in list(evidence.error_samples)]
    result, diagnostic = calibrate_forecast(
        [forecast()], evidence, now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result == [forecast()]
    assert all(band["covered_hours"] == 0 for band in diagnostic["lead_bands"].values())


def test_partial_quarters_of_same_hour_supply_duration_not_four_independent_hours():
    evidence = history([.5] * 168)
    split = []
    for row in evidence.error_samples:
        start = datetime.fromisoformat(row["start"])
        for quarter in range(4):
            split.append(dict(row, start=(start + timedelta(minutes=15 * quarter)).isoformat(),
                              end=(start + timedelta(minutes=15 * (quarter + 1))).isoformat()))
    evidence.error_samples = split
    result, diagnostic = calibrate_forecast(
        [forecast()], evidence, now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result[0].price == pytest.approx(1.6)
    assert diagnostic["lead_bands"]["24_48h"]["covered_hours"] == 168


def test_negative_forecasts_and_correction_cap_preserve_manual_uncertainty_floor():
    slot = forecast(price=-.5, uncertainty=.1)
    result, diagnostic = calibrate_forecast(
        [slot], history([2.0] * 168), now=NOW, enabled=True, uncertainty_floor=.6,
    )
    assert -.5 - .75 <= result[0].price < -.5
    assert result[0].uncertainty_dkk_per_kwh >= .6
    assert diagnostic["status"] == "active"


@pytest.mark.parametrize("bias", [-.5, .5])
def test_calibration_interval_contains_raw_bounds_in_both_correction_directions(bias):
    slot = forecast()
    result, diagnostic = calibrate_forecast(
        [slot], history([bias] * 168), now=NOW, enabled=True, uncertainty_floor=.25,
    )
    assert result[0].charge_price_dkk_per_kwh >= slot.charge_price_dkk_per_kwh - 1e-9
    assert result[0].discharge_price_dkk_per_kwh <= slot.discharge_price_dkk_per_kwh + 1e-9
    assert result[0].price != slot.price
    assert diagnostic["uncertainty_policy"] == "contains_original_source_interval"


@pytest.mark.parametrize("floor", [-1, float("nan"), float("inf")])
def test_invalid_floor_rejected(floor):
    with pytest.raises(ValueError):
        calibrate_forecast([forecast()], ForecastAccuracy(), now=NOW, enabled=True,
                           uncertainty_floor=floor)
