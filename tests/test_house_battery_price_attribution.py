"""Only confirmed, dated import prices settle measured energy."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components"))

from house_battery.coordinator import Fbp1200Coordinator
from house_battery.models import PriceSlot
from house_battery.price import diagnose_price_sources

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize("price", [0, -2, 3])
def test_confirmed_current_row_is_valid_even_at_zero_or_negative_price(price):
    slot = PriceSlot(NOW, NOW + timedelta(minutes=15), price)
    assert Fbp1200Coordinator._current_price(object(), [slot], NOW) == price


@pytest.mark.parametrize("price", ["nan", "inf", "1000", "2"])
def test_scalar_entity_state_cannot_replace_missing_dated_tariff(price):
    coord = SimpleNamespace(
        config={"price_entities": ["sensor.price"]},
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(state=price))),
    )
    assert Fbp1200Coordinator._current_price(coord, [], NOW) is None


@pytest.mark.parametrize("source,price", [("forecast", 2), ("known", float("nan")),
                                        ("known", 101), ("known", -21)])
def test_unconfirmed_or_invalid_rows_never_settle_actual_energy(source, price):
    coord = SimpleNamespace(config={"price_entities": []})
    slot = PriceSlot(NOW, NOW + timedelta(minutes=15), price, source=source)
    assert Fbp1200Coordinator._current_price(coord, [slot], NOW) is None


def test_expired_row_is_not_current_tariff():
    coord = SimpleNamespace(config={"price_entities": []})
    slot = PriceSlot(NOW - timedelta(minutes=15), NOW, 2)
    assert Fbp1200Coordinator._current_price(coord, [slot], NOW) is None


def test_source_diagnostics_do_not_convert_prices_or_infer_fee_completeness():
    def attributes(unit, price):
        return {"unit_of_measurement": unit, "prices": [{"start": NOW,
                "end": NOW + timedelta(minutes=15), "price": price}]}
    sources = {"known": attributes("kr/kWh", 2), "forecast": attributes("EUR/MWh", 30)}
    diagnostics = diagnose_price_sources(sources)
    assert diagnostics["sources"]["known"]["unit_status"] == "unverified"
    assert diagnostics["sources"]["forecast"]["unit_status"] == "incompatible"
    assert diagnostics["conflict_count"] == 1
    assert sources["forecast"]["prices"][0]["price"] == 30
    sources["forecast"] = attributes("DKK/kWh", 2)
    assert diagnose_price_sources(sources)["conflict_count"] == 0
