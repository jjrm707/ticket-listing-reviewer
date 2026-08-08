from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ticket_reviewer.domain.comparables import (
    comparable_weight,
    select_comparables,
    weighted_quantile,
)
from ticket_reviewer.domain.enums import ObservationKind, Source
from ticket_reviewer.domain.models import Comparable
from tests.factories import make_observation


NOW = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)


def observation(**overrides):
    values = {"observed_at": NOW - timedelta(minutes=10)}
    values.update(overrides)
    return make_observation(**values)


def comparable(price, relevance="1", quality="1"):
    return Comparable(
        source=Source.STUBHUB,
        event_external_id="event-1",
        kind=ObservationKind.LISTING,
        currency="USD",
        price_basis=Decimal(price),
        proceeds_basis=Decimal(price),
        section="132",
        observed_at=NOW,
        relevance=Decimal(relevance),
        quality=Decimal(quality),
    )


@pytest.mark.parametrize(
    ("candidate_overrides", "other_overrides", "expected"),
    [
        ({}, {}, Decimal("1.00")),
        ({}, {"row": "25"}, Decimal("0.80")),
        ({}, {"section": "133"}, Decimal("0.50")),
        (
            {},
            {"section": None, "kind": ObservationKind.EVENT_AGGREGATE},
            Decimal("0.35"),
        ),
        (
            {},
            {"section": None, "kind": ObservationKind.EVENT_FLOOR},
            Decimal("0.20"),
        ),
    ],
)
def test_comparable_weight_uses_exact_conservative_match_weights(
    candidate_overrides, other_overrides, expected
):
    candidate = observation(**candidate_overrides)
    other = observation(listing_id="other", **other_overrides)

    assert comparable_weight(candidate, other) == expected


def test_select_comparables_filters_unusable_evidence_and_normalizes_cents():
    candidate = observation(listing_id="candidate")
    accepted = observation(
        source=Source.SEATGEEK,
        listing_id="accepted",
        pair_price=Decimal("301.235"),
        section=" 132 ",
        row="24",
    )
    rejected = (
        replace(accepted, event_external_id="another-event", listing_id="event"),
        replace(accepted, currency="CAD", listing_id="currency"),
        replace(accepted, pair_price=None, listing_id="missing"),
        replace(accepted, pair_price=Decimal("0"), listing_id="zero"),
        replace(accepted, observed_at=NOW - timedelta(hours=24, seconds=1), listing_id="stale"),
        replace(accepted, observed_at=NOW + timedelta(minutes=6), listing_id="future"),
        replace(accepted, section="Parking Lot A", listing_id="parking-section"),
        replace(accepted, row="Parking Pass", listing_id="parking-row"),
        candidate,
    )

    selected = select_comparables(candidate, (accepted, *rejected), now=NOW)

    assert len(selected) == 1
    assert selected[0].price_basis == Decimal("301.24")
    assert selected[0].proceeds_basis == Decimal("301.24")
    assert selected[0].relevance == Decimal("1.00")
    assert selected[0].quality == Decimal("1.00")


def test_event_level_evidence_has_lower_quality_than_listings():
    candidate = observation(listing_id="candidate")
    selected = select_comparables(
        candidate,
        (
            observation(listing_id="listing"),
            observation(kind=ObservationKind.EVENT_AGGREGATE, listing_id=None),
            observation(kind=ObservationKind.EVENT_FLOOR, listing_id=None),
        ),
        now=NOW,
    )

    quality_by_kind = {item.kind: item.quality for item in selected}
    assert quality_by_kind == {
        ObservationKind.LISTING: Decimal("1.00"),
        ObservationKind.EVENT_AGGREGATE: Decimal("0.70"),
        ObservationKind.EVENT_FLOOR: Decimal("0.50"),
    }


def test_optional_now_uses_newest_supplied_observation_as_reference():
    candidate = observation(listing_id="candidate")
    newest = observation(observed_at=NOW + timedelta(days=3), listing_id="newest")
    old = observation(observed_at=NOW + timedelta(days=1, hours=23), listing_id="old")

    selected = select_comparables(candidate, (old, newest))

    assert tuple(item.price_basis for item in selected) == (Decimal("220.00"),)
    assert selected[0].observed_at == newest.observed_at


def test_weighted_quantile_uses_relevance_times_quality_and_exact_boundaries():
    values = (
        comparable("100", relevance="1", quality="0.20"),
        comparable("200", relevance="1", quality="0.30"),
        comparable("300", relevance="1", quality="0.50"),
    )

    assert weighted_quantile(values, Decimal("0")) == Decimal("100.00")
    assert weighted_quantile(values, Decimal("0.40")) == Decimal("200.00")
    assert weighted_quantile(values, Decimal("1")) == Decimal("300.00")


def test_weighted_quantile_cent_normalizes_the_selected_price():
    assert weighted_quantile((comparable("10.005"),)) == Decimal("10.01")


@pytest.mark.parametrize(
    ("values", "quantile"),
    [
        ((), Decimal("0.40")),
        ((comparable("10", relevance="0"),), Decimal("0.40")),
        ((comparable("10"),), Decimal("-0.01")),
        ((comparable("10"),), Decimal("1.01")),
        ((comparable("10"),), Decimal("NaN")),
        ((comparable("10"),), 0.4),
    ],
)
def test_weighted_quantile_rejects_invalid_or_empty_evidence(values, quantile):
    with pytest.raises(ValueError):
        weighted_quantile(values, quantile)
