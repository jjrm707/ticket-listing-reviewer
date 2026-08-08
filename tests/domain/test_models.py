from datetime import datetime
from decimal import Decimal

import pytest

from ticket_reviewer.domain.enums import Source
from ticket_reviewer.domain.models import ExitScenario
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
