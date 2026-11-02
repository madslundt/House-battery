"""Synthetic causal replay of external residual calibration.

This checks implementation behavior and delayed regime response. It does not
represent an archived external DK2 forecast or establish household savings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from random import Random
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from house_battery.forecast import ForecastAccuracy
from house_battery.forecast_calibration import calibrate_forecast
from house_battery.models import PriceSlot


def replay(errors: list[float], *, lead_hours: int = 36) -> dict:
    """Issue first, score only once target actuals become available, then refit."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    hour = timedelta(hours=1)
    pending, raw_errors, corrected_errors = [], [], []
    applied, tested, envelope_violations = 0, 0, 0
    reasons = {}
    for index, error in enumerate(errors):
        now = start + hour * index
        evidence = [row for row in pending
                    if datetime.fromisoformat(row["scored_at"]) <= now
                    and datetime.fromisoformat(row["end"]) > now - timedelta(days=7)]
        target = now + hour * lead_hours
        # The observed outcome is fixed; only forecast residual behavior varies.
        actual_price = 1.0
        slot = PriceSlot(target, target + hour, actual_price + error, source="forecast",
                         uncertainty_dkk_per_kwh=.25)
        calibrated, diagnostic = calibrate_forecast(
            [slot], ForecastAccuracy(error_samples=evidence), now=now,
            enabled=True, uncertainty_floor=.25,
        )
        if index >= 10 * 24:
            tested += 1
            raw_errors.append(abs(error))
            corrected_errors.append(abs(calibrated[0].price - actual_price))
            applied += diagnostic["corrected_slots"]
            envelope_violations += (
                calibrated[0].charge_price_dkk_per_kwh < slot.charge_price_dkk_per_kwh - 1e-9
                or calibrated[0].discharge_price_dkk_per_kwh > slot.discharge_price_dkk_per_kwh + 1e-9
            )
            band = "0_24h" if lead_hours < 24 else "24_48h" if lead_hours < 48 else "48_72h"
            reason = diagnostic["lead_bands"][band]["reason"]
            reasons[reason] = reasons.get(reason, 0) + 1
        pending.append({
            "start": target.isoformat(), "end": (target + hour).isoformat(),
            "issued_at": now.isoformat(), "scored_at": (target + hour).isoformat(),
            "error_dkk_per_kwh": error,
        })
    return {
        "scored_forecast_instances": tested, "lead_hours": lead_hours,
        "raw_mae_dkk_per_kwh": mean(raw_errors),
        "calibrated_mae_dkk_per_kwh": mean(corrected_errors),
        "raw_max_error_dkk_per_kwh": max(raw_errors),
        "calibrated_max_error_dkk_per_kwh": max(corrected_errors),
        "corrected_instances": applied, "gate_reasons": reasons,
        "original_interval_envelope_violations": envelope_violations,
    }


def main() -> None:
    random = Random(20261102)
    count = 21 * 24
    cases = {
        "stable_positive_bias": ([.4] * count, 36),
        "stable_negative_bias_long_lead": ([-.4] * count, 60),
        "zero_mean_noise": ([random.uniform(-.4, .4) for _ in range(count)], 36),
        "bias_reversal_after_day14": ([.4] * (14 * 24) + [-.4] * (7 * 24), 36),
    }
    print(json.dumps({
        "method": "21 days of hourly synthetic issues; first 10 days warmup; actual residual known only after target hour; seven-day source history",
        "limitation": "Implementation simulation only; no archived external forecast or household-savings evidence",
        "cases": {name: replay(errors, lead_hours=lead) for name, (errors, lead) in cases.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
