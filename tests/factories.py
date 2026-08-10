from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from ticket_reviewer.domain.enums import Confidence, ObservationKind, Source, Team
from ticket_reviewer.domain.models import (
    ExitScenario,
    ExternalEvent,
    OpportunityEstimate,
    SourceObservation,
)


DEFAULT_STARTS_AT = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)


def make_event(**overrides: object) -> ExternalEvent:
    event = ExternalEvent(
        source=Source.STUBHUB,
        external_id="texans-vs-opponent-2026-09-13",
        team=Team.TEXANS,
        opponent="Opponent",
        venue="NRG Stadium",
        starts_at=DEFAULT_STARTS_AT,
        is_home=True,
        is_parking=False,
        url="https://example.test/events/texans-vs-opponent",
    )
    return replace(event, **overrides)


def make_observation(**overrides: object) -> SourceObservation:
    observation = SourceObservation(
        source=Source.STUBHUB,
        event_external_id="texans-vs-opponent-2026-09-13",
        observed_at=DEFAULT_STARTS_AT,
        kind=ObservationKind.LISTING,
        currency="USD",
        pair_price=Decimal("220.00"),
        buyer_fees=Decimal("25.00"),
        estimated_tax=Decimal("18.15"),
        section="132",
        row="24",
        quantity_available=2,
        can_buy_pair=True,
        listing_id="listing-123",
        listing_url="https://example.test/listings/listing-123",
    )
    return replace(observation, **overrides)


def make_estimate(**overrides: object) -> OpportunityEstimate:
    scenario = ExitScenario(
        marketplace=Source.STUBHUB,
        projected_resale_gross=Decimal("300.00"),
        seller_fee_rate=Decimal("0.15"),
        projected_proceeds=Decimal("255.00"),
        comparable_count=0,
    )
    estimate = OpportunityEstimate(
        acquisition_total=Decimal("263.15"),
        exit_source=Source.STUBHUB,
        projected_resale_gross=Decimal("300.00"),
        seller_fee_rate=Decimal("0.15"),
        projected_proceeds=Decimal("255.00"),
        estimated_net_profit=Decimal("-8.15"),
        roi=Decimal("-0.0310"),
        confidence=Confidence.MEDIUM,
        risk_reasons=("limited comparables",),
        actionable=False,
        scenarios=(scenario,),
    )
    return replace(estimate, **overrides)
