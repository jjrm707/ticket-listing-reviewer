from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ticket_reviewer.domain.enums import Confidence, ObservationKind, Source
from ticket_reviewer.domain.scoring import estimate_exit_scenarios, estimate_opportunity
from tests.factories import make_observation


NOW = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)
FEES = {
    Source.STUBHUB: Decimal("0.15"),
    Source.TICKETMASTER: Decimal("0.18"),
    Source.SEATGEEK: Decimal("0.15"),
}


def test_exit_scenario_records_sorted_unique_exact_comparable_ids():
    candidate = make_observation(
        event_external_id="event-1",
        listing_id="candidate",
        observation_id=9,
        observed_at=NOW,
    )
    comparables = (
        make_observation(
            event_external_id="event-1",
            listing_id="first",
            observation_id=8,
            source=Source.STUBHUB,
            observed_at=NOW,
        ),
        make_observation(
            event_external_id="event-1",
            listing_id="second",
            observation_id=3,
            source=Source.STUBHUB,
            observed_at=NOW,
        ),
    )

    scenarios = estimate_exit_scenarios(
        candidate,
        (candidate, *comparables),
        {Source.STUBHUB: Decimal("0.15")},
        now=NOW,
    )

    assert scenarios[0].comparable_observation_ids == (3, 8)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def candidate(now):
    return make_observation(
        event_external_id="event-1",
        observed_at=now - timedelta(minutes=10),
        pair_price=Decimal("200.00"),
        buyer_fees=Decimal("10.00"),
        estimated_tax=Decimal("10.00"),
        listing_id="candidate",
        popularity=Decimal("0.50"),
    )


def comp(candidate, index, price, *, source=Source.STUBHUB, minutes=5, **overrides):
    values = {
        "source": source,
        "observed_at": NOW - timedelta(minutes=minutes),
        "pair_price": Decimal(price),
        "buyer_fees": Decimal("0"),
        "estimated_tax": Decimal("0"),
        "listing_id": f"comp-{source.value}-{index}",
        "popularity": Decimal("0.50"),
    }
    values.update(overrides)
    return replace(candidate, **values)


@pytest.fixture
def comparable_observations(candidate):
    return (
        comp(candidate, 1, "300", source=Source.STUBHUB),
        comp(candidate, 2, "330", source=Source.STUBHUB),
        comp(candidate, 3, "400", source=Source.STUBHUB),
        comp(candidate, 4, "330", source=Source.TICKETMASTER),
        comp(candidate, 5, "340", source=Source.TICKETMASTER),
        comp(candidate, 6, "350", source=Source.TICKETMASTER),
    )


@pytest.fixture
def event_floor_only(candidate):
    return (
        comp(
            candidate,
            1,
            "300",
            kind=ObservationKind.EVENT_FLOOR,
            section=None,
            row=None,
            listing_id=None,
        ),
    )


def stable_comparables(candidate, **overrides):
    return tuple(comp(candidate, index, "300", minutes=index, **overrides) for index in range(1, 4))


def test_uses_lower_weighted_market_value(candidate, comparable_observations, now):
    estimate = estimate_opportunity(
        candidate,
        comparable_observations,
        fee_profiles=FEES,
        now=now,
        budget_cap=Decimal("400.00"),
    )
    assert estimate.exit_source is Source.STUBHUB
    assert estimate.projected_resale_gross == Decimal("330.00")
    assert estimate.projected_proceeds == Decimal("280.50")
    assert estimate.estimated_net_profit == Decimal("60.50")
    assert estimate.roi == Decimal("0.2750")


def test_sparse_event_level_data_is_low_confidence(candidate, event_floor_only, now):
    estimate = estimate_opportunity(
        candidate,
        event_floor_only,
        {Source.STUBHUB: Decimal("0.15")},
        now,
        Decimal("400.00"),
    )
    assert estimate.confidence is Confidence.LOW
    assert "fewer than 3 seat-level comparables" in estimate.risk_reasons


def test_exit_scenarios_apply_marketplace_fees_and_use_deterministic_ties(candidate):
    observations = stable_comparables(candidate) + stable_comparables(
        candidate, source=Source.SEATGEEK
    )

    scenarios = estimate_exit_scenarios(
        candidate,
        observations,
        {Source.STUBHUB: Decimal("0.15"), Source.SEATGEEK: Decimal("0.15")},
        now=NOW,
    )
    estimate = estimate_opportunity(
        candidate,
        observations,
        {Source.STUBHUB: Decimal("0.15"), Source.SEATGEEK: Decimal("0.15")},
        NOW,
        Decimal("400"),
    )

    assert tuple(item.marketplace for item in scenarios) == (
        Source.SEATGEEK,
        Source.STUBHUB,
    )
    assert scenarios[0].projected_proceeds == Decimal("255.00")
    assert estimate.exit_source is Source.SEATGEEK


def test_manual_only_evidence_cannot_create_an_actionable_exit(candidate):
    observations = stable_comparables(candidate, source=Source.MANUAL)
    fees = {Source.MANUAL: Decimal("0")}

    assert estimate_exit_scenarios(candidate, observations, fees, now=NOW) == ()

    estimate = estimate_opportunity(
        candidate, observations, fees, NOW, Decimal("400")
    )
    assert estimate.scenarios == ()
    assert estimate.exit_source is None
    assert estimate.actionable is False


def test_mixed_evidence_never_selects_manual_as_the_exit_marketplace(candidate):
    observations = stable_comparables(candidate) + tuple(
        comp(candidate, index, "1000", source=Source.MANUAL)
        for index in range(4, 7)
    )

    estimate = estimate_opportunity(
        candidate,
        observations,
        {Source.STUBHUB: Decimal("0.15"), Source.MANUAL: Decimal("0")},
        NOW,
        Decimal("400"),
    )

    assert tuple(scenario.marketplace for scenario in estimate.scenarios) == (
        Source.STUBHUB,
    )
    assert estimate.exit_source is Source.STUBHUB


@pytest.mark.parametrize(
    "fees",
    (
        {},
        {Source.STUBHUB: None},
        {Source.STUBHUB: 0.15},
        {Source.STUBHUB: Decimal("NaN")},
        {Source.STUBHUB: Decimal("-0.01")},
        {Source.STUBHUB: Decimal("1.01")},
    ),
)
def test_missing_or_invalid_seller_fee_never_fabricates_a_scenario(candidate, fees):
    estimate = estimate_opportunity(
        candidate, stable_comparables(candidate), fees, NOW, Decimal("400")
    )

    assert estimate.scenarios == ()
    assert estimate.exit_source is None
    assert estimate.estimated_net_profit is None
    assert estimate.actionable is False
    assert "no valid exit scenario" in estimate.risk_reasons


def test_cross_event_stale_parking_and_future_evidence_cannot_create_an_exit(candidate):
    base = comp(candidate, 1, "300")
    observations = (
        replace(base, event_external_id="other"),
        replace(base, observed_at=NOW - timedelta(hours=25)),
        replace(base, observed_at=NOW + timedelta(minutes=6)),
        replace(base, section="Parking Pass"),
    )

    assert estimate_exit_scenarios(candidate, observations, FEES, now=NOW) == ()


def test_exit_scenarios_ignore_positive_prices_that_round_to_zero(candidate):
    observations = (comp(candidate, 1, "0.001"),)

    assert estimate_exit_scenarios(candidate, observations, FEES, now=NOW) == ()


def test_opportunity_ignores_positive_prices_that_round_to_zero(candidate):
    observations = (comp(candidate, 1, "0.001"),)

    estimate = estimate_opportunity(
        candidate, observations, FEES, NOW, Decimal("400")
    )

    assert estimate.scenarios == ()
    assert estimate.exit_source is None
    assert estimate.actionable is False


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ("sparse", "fewer than 3 seat-level comparables"),
        ("section", "seat quality unverified"),
        ("row", "seat quality unverified"),
        ("buyer_fees", "buyer fees unavailable"),
        ("tax", "estimated tax unavailable"),
        ("stale", "newest comparable is older than 2 hours"),
        ("manual", "candidate source is manually corrected OCR"),
    ],
)
def test_each_confidence_risk_group_drops_high_to_medium(
    candidate, change, expected_reason
):
    observations = stable_comparables(candidate)
    changed_candidate = candidate
    if change == "sparse":
        observations = observations[:2]
    elif change == "section":
        observations = tuple(replace(item, section="999") for item in observations)
    elif change == "row":
        changed_candidate = replace(candidate, row=None)
    elif change == "buyer_fees":
        changed_candidate = replace(candidate, buyer_fees=None)
    elif change == "tax":
        changed_candidate = replace(candidate, estimated_tax=None)
    elif change == "stale":
        observations = tuple(
            replace(item, observed_at=NOW - timedelta(hours=3, minutes=index))
            for index, item in enumerate(observations)
        )
    elif change == "manual":
        changed_candidate = replace(candidate, source=Source.MANUAL)

    estimate = estimate_opportunity(
        changed_candidate, observations, FEES, NOW, Decimal("400")
    )

    assert estimate.confidence is Confidence.MEDIUM
    assert expected_reason in estimate.risk_reasons


def test_multiple_risk_groups_clamp_confidence_at_low(candidate):
    changed = replace(candidate, source=Source.MANUAL, buyer_fees=None, row=None)
    estimate = estimate_opportunity(
        changed, stable_comparables(candidate)[:1], FEES, NOW, Decimal("400")
    )

    assert estimate.confidence is Confidence.LOW
    assert len(estimate.risk_reasons) == len(set(estimate.risk_reasons))


def test_whitespace_only_row_is_unknown_seat_quality(candidate):
    changed = replace(candidate, row=" \t ")

    estimate = estimate_opportunity(
        changed, stable_comparables(candidate), FEES, NOW, Decimal("400")
    )

    assert estimate.confidence is Confidence.MEDIUM
    assert "seat quality unverified" in estimate.risk_reasons


def test_missing_acquisition_costs_are_zero_only_with_explicit_uncertainty(candidate):
    changed = replace(candidate, buyer_fees=None, estimated_tax=None)
    estimate = estimate_opportunity(
        changed, stable_comparables(candidate), FEES, NOW, Decimal("400")
    )

    assert estimate.acquisition_total == Decimal("200.00")
    assert estimate.confidence is Confidence.MEDIUM
    assert "buyer fees unavailable" in estimate.risk_reasons
    assert "estimated tax unavailable" in estimate.risk_reasons


def test_event_level_candidate_is_visible_but_never_actionable(candidate):
    changed = replace(candidate, kind=ObservationKind.EVENT_FLOOR)
    estimate = estimate_opportunity(
        changed, stable_comparables(candidate), FEES, NOW, Decimal("400")
    )

    assert estimate.scenarios
    assert estimate.actionable is False
    assert "pair availability is not confirmed" in estimate.risk_reasons


def test_sparse_comparables_receive_only_the_ten_percent_haircut(candidate):
    observations = stable_comparables(candidate)[:2]
    estimate = estimate_opportunity(
        candidate, observations, FEES, NOW, Decimal("400")
    )

    assert estimate.projected_resale_gross == Decimal("270.00")
    assert "sparse comparable haircut applied" in estimate.risk_reasons


def test_blank_sections_keep_high_asks_conservative_and_lower_confidence(candidate):
    changed = replace(candidate, section=" \t ", row="24")
    observations = (
        comp(changed, 1, "300", section="132", row="24"),
        comp(changed, 2, "500", section="\n", row="24"),
    )

    scenarios = estimate_exit_scenarios(changed, observations, FEES, now=NOW)
    estimate = estimate_opportunity(
        changed, observations, FEES, NOW, Decimal("400")
    )

    assert scenarios[0].projected_resale_gross == Decimal("300.00")
    assert estimate.projected_resale_gross == Decimal("270.00")
    assert estimate.projected_proceeds == Decimal("229.50")
    assert estimate.estimated_net_profit == Decimal("9.50")
    assert estimate.confidence is Confidence.LOW
    assert "seat quality unverified" in estimate.risk_reasons


def test_more_than_ten_percent_three_snapshot_decline_receives_five_percent_haircut(candidate):
    observations = (
        comp(candidate, 1, "400", minutes=180),
        comp(candidate, 2, "360", minutes=120),
        comp(candidate, 3, "350", minutes=60),
    )
    estimate = estimate_opportunity(
        candidate, observations, FEES, NOW, Decimal("400")
    )

    assert estimate.projected_resale_gross == Decimal("342.00")
    assert "declining comparable prices" in estimate.risk_reasons


def test_upward_trend_and_high_popularity_never_raise_projected_value(candidate):
    observations = (
        comp(candidate, 1, "300", minutes=180, popularity=Decimal("0.90")),
        comp(candidate, 2, "330", minutes=120, popularity=Decimal("0.90")),
        comp(candidate, 3, "360", minutes=60, popularity=Decimal("0.90")),
    )
    estimate = estimate_opportunity(
        candidate,
        observations,
        FEES,
        NOW,
        Decimal("400"),
        kickoff_at=NOW + timedelta(days=10),
    )

    assert estimate.projected_resale_gross == Decimal("330.00")


def test_missing_popularity_is_standard_with_explicit_risk(candidate):
    observations = tuple(
        replace(item, popularity=None) for item in stable_comparables(candidate)
    )
    estimate = estimate_opportunity(
        candidate, observations, FEES, NOW, Decimal("400")
    )

    assert estimate.projected_resale_gross == Decimal("300.00")
    assert "opponent demand unverified" in estimate.risk_reasons


def test_missing_kickoff_is_not_guessed_from_observed_at(candidate):
    observations = stable_comparables(candidate)
    estimate = estimate_opportunity(
        candidate, observations, FEES, NOW, Decimal("400")
    )

    assert estimate.projected_resale_gross == Decimal("300.00")
    assert "kickoff proximity unverified" in estimate.risk_reasons


def test_nonpositive_trend_within_seven_days_receives_five_percent_haircut(candidate):
    estimate = estimate_opportunity(
        candidate,
        stable_comparables(candidate),
        FEES,
        NOW,
        Decimal("400"),
        kickoff_at=NOW + timedelta(days=7),
    )

    assert estimate.projected_resale_gross == Decimal("285.00")
    assert "nonpositive trend within 7 days of kickoff" in estimate.risk_reasons


def test_high_inventory_within_three_days_adds_a_second_five_percent_haircut(candidate):
    observations = tuple(
        replace(item, listing_count=501) for item in stable_comparables(candidate)
    )
    estimate = estimate_opportunity(
        candidate,
        observations,
        FEES,
        NOW,
        Decimal("400"),
        kickoff_at=NOW + timedelta(days=3),
    )

    assert estimate.projected_resale_gross == Decimal("270.75")
    assert "high inventory within 3 days of kickoff" in estimate.risk_reasons


def test_fewer_than_three_snapshots_does_not_invent_decline_haircut(candidate):
    observations = (
        comp(candidate, 1, "300", minutes=120),
        comp(candidate, 2, "300", minutes=60),
        comp(candidate, 3, "300", minutes=60),
    )
    estimate = estimate_opportunity(
        candidate,
        observations,
        FEES,
        NOW,
        Decimal("400"),
        kickoff_at=NOW + timedelta(days=10),
    )

    assert estimate.projected_resale_gross == Decimal("300.00")
    assert "declining comparable prices" not in estimate.risk_reasons


def test_risk_reason_order_is_deterministic(candidate):
    changed = replace(
        candidate,
        source=Source.MANUAL,
        buyer_fees=None,
        estimated_tax=None,
        row=None,
        popularity=None,
    )
    estimate = estimate_opportunity(changed, (), {}, NOW, Decimal("400"))

    assert estimate.risk_reasons == (
        "buyer fees unavailable",
        "estimated tax unavailable",
        "fewer than 3 seat-level comparables",
        "seat quality unverified",
        "candidate source is manually corrected OCR",
        "opponent demand unverified",
        "kickoff proximity unverified",
        "sparse comparable haircut applied",
        "price trend unverified",
        "no valid exit scenario",
    )


@pytest.mark.parametrize("budget_cap", (400.0, Decimal("NaN"), Decimal("Infinity")))
def test_invalid_budget_cap_never_becomes_actionable(candidate, budget_cap):
    estimate = estimate_opportunity(
        candidate, stable_comparables(candidate), FEES, NOW, budget_cap
    )

    assert estimate.actionable is False
    assert "budget cap is invalid" in estimate.risk_reasons


def test_now_and_kickoff_must_be_timezone_aware(candidate):
    naive = datetime(2026, 8, 7, 18, 0)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        estimate_opportunity(candidate, (), FEES, naive, Decimal("400"))
    with pytest.raises(ValueError, match="kickoff_at must be timezone-aware"):
        estimate_opportunity(
            candidate, (), FEES, NOW, Decimal("400"), kickoff_at=naive
        )
