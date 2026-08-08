from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ticket_reviewer.domain.enums import ObservationKind, Source
from ticket_reviewer.domain.models import Comparable, ExitScenario
from tests.factories import make_estimate, make_event, make_observation


def test_event_rejects_naive_start_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        make_event(starts_at=datetime(2026, 9, 13, 17, 0))


def test_observation_rejects_naive_observed_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        make_observation(observed_at=datetime(2026, 9, 13, 17, 0))


def test_observation_normalizes_currency_to_uppercase():
    observation = make_observation(currency="usd")

    assert observation.currency == "USD"


def test_observation_rejects_negative_price():
    with pytest.raises(ValueError, match="pair_price"):
        make_observation(pair_price=Decimal("-0.01"))


def test_observation_rejects_binary_float_price():
    with pytest.raises(ValueError, match="pair_price must be a Decimal"):
        make_observation(pair_price=220.0)


def test_exit_scenario_rejects_negative_comparable_count():
    with pytest.raises(ValueError, match="comparable_count"):
        ExitScenario(
            marketplace=Source.STUBHUB,
            projected_resale_gross=Decimal("300.00"),
            seller_fee_rate=Decimal("0.15"),
            projected_proceeds=Decimal("255.00"),
            comparable_count=-1,
        )


def test_estimate_rejects_negative_acquisition_total():
    with pytest.raises(ValueError, match="acquisition_total"):
        make_estimate(acquisition_total=Decimal("-0.01"))


@pytest.mark.parametrize("field_name", ("quantity_available", "listing_count"))
@pytest.mark.parametrize("value", (1.5, True))
def test_observation_rejects_non_integer_counts(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        make_observation(**{field_name: value})


@pytest.mark.parametrize("value", (1.5, True))
def test_exit_scenario_rejects_non_integer_comparable_count(value):
    with pytest.raises(ValueError, match="comparable_count"):
        ExitScenario(
            marketplace=Source.STUBHUB,
            projected_resale_gross=Decimal("300.00"),
            seller_fee_rate=Decimal("0.15"),
            projected_proceeds=Decimal("255.00"),
            comparable_count=value,
        )


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity")))
@pytest.mark.parametrize(
    "field_name", ("pair_price", "buyer_fees", "estimated_tax", "popularity")
)
def test_observation_rejects_nonfinite_decimal_values(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        make_observation(**{field_name: value})


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity")))
@pytest.mark.parametrize(
    "field_name", ("price_basis", "proceeds_basis", "relevance", "quality")
)
def test_comparable_rejects_nonfinite_decimal_values(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        Comparable(**{
            "source": Source.STUBHUB,
            "event_external_id": "texans-vs-opponent-2026-09-13",
            "kind": ObservationKind.LISTING,
            "currency": "USD",
            "price_basis": Decimal("220.00"),
            "proceeds_basis": Decimal("187.00"),
            "section": "132",
            "observed_at": datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
            "relevance": Decimal("1"),
            "quality": Decimal("1"),
        } | {field_name: value})


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity")))
@pytest.mark.parametrize(
    "field_name",
    (
        "projected_resale_gross",
        "seller_fee_rate",
        "projected_proceeds",
    ),
)
def test_exit_scenario_rejects_nonfinite_decimal_values(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        ExitScenario(**{
            "marketplace": Source.STUBHUB,
            "projected_resale_gross": Decimal("300.00"),
            "seller_fee_rate": Decimal("0.15"),
            "projected_proceeds": Decimal("255.00"),
            "comparable_count": 3,
        } | {field_name: value})


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity")))
@pytest.mark.parametrize(
    "field_name",
    (
        "acquisition_total",
        "projected_resale_gross",
        "seller_fee_rate",
        "projected_proceeds",
        "estimated_net_profit",
        "roi",
    ),
)
def test_estimate_rejects_nonfinite_decimal_values(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        make_estimate(**{field_name: value})


@pytest.mark.parametrize(
    "field_name", ("pair_price", "buyer_fees", "estimated_tax", "popularity")
)
def test_observation_rejects_float_decimal_values(field_name):
    with pytest.raises(ValueError, match=field_name):
        make_observation(**{field_name: 1.0})


@pytest.mark.parametrize(
    "field_name", ("price_basis", "proceeds_basis", "relevance", "quality")
)
def test_comparable_rejects_float_decimal_values(field_name):
    with pytest.raises(ValueError, match=field_name):
        Comparable(**{
            "source": Source.STUBHUB,
            "event_external_id": "texans-vs-opponent-2026-09-13",
            "kind": ObservationKind.LISTING,
            "currency": "USD",
            "price_basis": Decimal("220.00"),
            "proceeds_basis": Decimal("187.00"),
            "section": "132",
            "observed_at": datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
            "relevance": Decimal("1"),
            "quality": Decimal("1"),
        } | {field_name: 1.0})


@pytest.mark.parametrize(
    "field_name",
    ("projected_resale_gross", "seller_fee_rate", "projected_proceeds"),
)
def test_exit_scenario_rejects_float_decimal_values(field_name):
    with pytest.raises(ValueError, match=field_name):
        ExitScenario(**{
            "marketplace": Source.STUBHUB,
            "projected_resale_gross": Decimal("300.00"),
            "seller_fee_rate": Decimal("0.15"),
            "projected_proceeds": Decimal("255.00"),
            "comparable_count": 3,
        } | {field_name: 1.0})


@pytest.mark.parametrize(
    "field_name",
    (
        "acquisition_total",
        "projected_resale_gross",
        "seller_fee_rate",
        "projected_proceeds",
        "estimated_net_profit",
        "roi",
    ),
)
def test_estimate_rejects_float_decimal_values(field_name):
    with pytest.raises(ValueError, match=field_name):
        make_estimate(**{field_name: 1.0})
