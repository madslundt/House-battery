"""Measured activity never values missing or malformed observations."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from homeassistant.helpers.json import json_dumps

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.actual_history import ActualDailyHistory


@pytest.mark.parametrize("invalid_price", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_prices_never_poison_persisted_activity(invalid_price: float) -> None:
    start = datetime(2026, 10, 29, 10, tzinfo=UTC)
    history = ActualDailyHistory()
    history.record(start, 50, 1000, 0, 1)
    history.record(start + timedelta(minutes=1), 50, 1000, 0, invalid_price)
    restored = ActualDailyHistory.from_dict(json.loads(json_dumps(history.as_dict())))

    restored.record(start + timedelta(minutes=2), 50, 1000, 0, 2)
    restored.record(start + timedelta(minutes=3), 50, 1000, 0, 2)

    blocks = restored.today(start)
    assert sum(block["charge_cost_dkk"] for block in blocks) == pytest.approx(1 / 30)
    assert sum(block["priced_charge_kwh"] for block in blocks) == pytest.approx(1 / 60)
    assert sum(block["charge_kwh"] for block in blocks) == pytest.approx(1 / 30)


def test_null_or_malformed_saved_numbers_do_not_break_the_next_observation() -> None:
    start = datetime(2026, 10, 29, 10, tzinfo=UTC)
    history = ActualDailyHistory.from_dict({
        "days": {start.date().isoformat(): [{
            "start": start.isoformat(), "end": start.isoformat(), "action": "charging",
            "charge_kwh": None, "discharge_kwh": "invalid", "charge_cost_dkk": None,
            "discharge_value_dkk": float("inf"), "priced_charge_kwh": None,
            "priced_discharge_kwh": None, "samples": None,
            "soc_start": None, "soc_end": 50, "soc_min": "invalid", "soc_max": float("nan"),
        }]},
        "last_sample_at": start.isoformat(), "sample_count": None,
    })

    history.record(start + timedelta(minutes=1), 50, 1000, 0, 2)
    history.record(start + timedelta(minutes=2), 50, 1000, 0, 2)

    block = history.today(start)[-1]
    assert block["charge_kwh"] == pytest.approx(1 / 60)
    assert block["charge_cost_dkk"] == pytest.approx(1 / 30)
    assert block["soc_min"] == block["soc_max"] == 50


def test_recovered_activity_does_not_value_the_preceding_unknown_gap() -> None:
    start = datetime(2026, 10, 29, 10, tzinfo=UTC)
    history = ActualDailyHistory()
    for minute in range(5):
        history.record(
            start + timedelta(minutes=minute), 50,
            None if minute == 2 else 1000, None if minute == 2 else 0, 2,
        )

    blocks = history.today(start)
    assert [block["action"] for block in blocks] == ["charging", "unknown", "charging"]
    assert sum(block["charge_kwh"] for block in blocks) == pytest.approx(1 / 30)
    assert sum(block["charge_cost_dkk"] for block in blocks) == pytest.approx(1 / 15)


@pytest.mark.parametrize("restored,resume_minute", [(True, 2), (True, 60), (False, 60)])
def test_offline_activity_is_unknown_and_unpriced(restored: bool, resume_minute: int) -> None:
    start = datetime(2026, 10, 29, 10, tzinfo=UTC)
    history = ActualDailyHistory()
    history.record(start, 50, 1000, 0, 1)
    history.record(start + timedelta(minutes=1), 50, 1000, 0, 1)
    if restored:
        history = ActualDailyHistory.from_dict(history.as_dict())

    history.record(start + timedelta(minutes=resume_minute), 60, 1000, 0, 5)
    history.record(start + timedelta(minutes=resume_minute + 1), 60, 1000, 0, 5)

    blocks = history.today(start)
    assert [block["action"] for block in blocks] == ["charging", "unknown", "charging"]
    assert blocks[0]["end"] == (start + timedelta(minutes=1)).isoformat()
    assert blocks[1]["start"] == (start + timedelta(minutes=1)).isoformat()
    assert blocks[1]["end"] == (start + timedelta(minutes=resume_minute)).isoformat()
    assert sum(block["charge_kwh"] for block in blocks) == pytest.approx(1 / 30)
    assert sum(block["charge_cost_dkk"] for block in blocks) == pytest.approx(0.1)
