from dataclasses import replace
from decimal import Decimal

import pytest

from ticket_reviewer.domain.eligibility import is_actionable_pair
from ticket_reviewer.domain.enums import ObservationKind
from tests.factories import make_observation


@pytest.fixture
def listing_observation():
    return make_observation()


@pytest.fixture
def event_floor_observation():
    return make_observation(kind=ObservationKind.EVENT_FLOOR)


def test_pair_at_budget_is_actionable(listing_observation):
    ok, reasons = is_actionable_pair(
        listing_observation, Decimal("400.00"), Decimal("400.00")
    )

    assert ok is True
    assert reasons == ()


def test_event_floor_is_not_a_confirmed_pair(event_floor_observation):
    ok, reasons = is_actionable_pair(
        event_floor_observation, Decimal("200.00"), Decimal("400.00")
    )

    assert ok is False
    assert reasons == ("pair availability is not confirmed",)


def test_non_listing_observation_is_not_a_confirmed_pair(listing_observation):
    aggregate = replace(listing_observation, kind=ObservationKind.EVENT_AGGREGATE)

    ok, reasons = is_actionable_pair(aggregate, Decimal("200.00"), Decimal("400.00"))

    assert ok is False
    assert "pair availability is not confirmed" in reasons


@pytest.mark.parametrize("can_buy_pair", (False, None, 1))
def test_pair_must_be_explicitly_purchaseable(listing_observation, can_buy_pair):
    observation = replace(listing_observation, can_buy_pair=can_buy_pair)

    ok, reasons = is_actionable_pair(observation, Decimal("200.00"), Decimal("400.00"))

    assert ok is False
    assert "pair cannot be purchased" in reasons


@pytest.mark.parametrize("quantity_available", (None, 0, 1))
def test_pair_requires_at_least_two_confirmed_tickets(listing_observation, quantity_available):
    observation = replace(listing_observation, quantity_available=quantity_available)

    ok, reasons = is_actionable_pair(observation, Decimal("200.00"), Decimal("400.00"))

    assert ok is False
    assert "at least two tickets are not confirmed available" in reasons


def test_pair_requires_listing_pair_price(listing_observation):
    observation = replace(listing_observation, pair_price=None)

    ok, reasons = is_actionable_pair(observation, Decimal("200.00"), Decimal("400.00"))

    assert ok is False
    assert "pair price is unavailable" in reasons


def test_pair_requires_usd_currency(listing_observation):
    observation = replace(listing_observation, currency="CAD")

    ok, reasons = is_actionable_pair(observation, Decimal("200.00"), Decimal("400.00"))

    assert ok is False
    assert "currency is not USD" in reasons


@pytest.mark.parametrize(
    "acquisition",
    (None, Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), 200.0),
)
def test_pair_rejects_missing_or_invalid_acquisition(listing_observation, acquisition):
    ok, reasons = is_actionable_pair(listing_observation, acquisition, Decimal("400.00"))

    assert ok is False
    assert "acquisition total is invalid" in reasons


@pytest.mark.parametrize(
    "budget_cap",
    (None, Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), 400.0),
)
def test_pair_rejects_invalid_budget_cap(listing_observation, budget_cap):
    ok, reasons = is_actionable_pair(listing_observation, Decimal("200.00"), budget_cap)

    assert ok is False
    assert "budget cap is invalid" in reasons


def test_pair_above_budget_is_not_actionable(listing_observation):
    ok, reasons = is_actionable_pair(
        listing_observation, Decimal("400.01"), Decimal("400.00")
    )

    assert ok is False
    assert reasons == ("acquisition total exceeds budget cap",)


def test_rejection_reasons_are_complete_and_deterministically_ordered(listing_observation):
    observation = replace(
        listing_observation,
        kind=ObservationKind.EVENT_FLOOR,
        can_buy_pair=False,
        quantity_available=1,
        pair_price=None,
        currency="CAD",
    )

    ok, reasons = is_actionable_pair(observation, Decimal("450.00"), Decimal("400.00"))

    assert ok is False
    assert reasons == (
        "pair availability is not confirmed",
        "pair cannot be purchased",
        "at least two tickets are not confirmed available",
        "pair price is unavailable",
        "currency is not USD",
        "acquisition total exceeds budget cap",
    )
