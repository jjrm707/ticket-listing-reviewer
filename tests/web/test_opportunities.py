from datetime import timedelta
from decimal import Decimal

import pytest

from .conftest import NOW, seed_opportunity
from ticket_reviewer.data.schema import ObservationRow


def test_opportunities_sorted_by_profit(client, seeded_opportunities):
    response = client.get("/")

    assert response.status_code == 200
    assert response.text.index("$82.00") < response.text.index("$51.00")


def test_low_confidence_is_visibly_labeled(client, low_confidence_opportunity):
    response = client.get("/")

    assert response.status_code == 200
    assert "Low confidence" in response.text
    assert "fewer than 3 seat-level comparables" in response.text


def test_filters_compose_with_and_semantics_and_remain_in_form(
    client, session_factory
):
    with session_factory() as session:
        event, _, _ = seed_opportunity(
            session,
            profit=Decimal("67.00"),
            confidence="low",
            opponent="Matching Opponent",
        )
        seed_opportunity(
            session,
            profit=Decimal("90.00"),
            confidence="high",
            opponent="Wrong Confidence",
        )
        event_id = event.id
        session.commit()

    response = client.get(
        "/",
        params={
            "team": "texans",
            "event_id": str(event_id),
            "source": "stubhub",
            "confidence": "low",
            "min_profit": "60.00",
            "max_cost": "230.00",
            "status": "new",
        },
    )

    assert response.status_code == 200
    assert "Matching Opponent" in response.text
    assert "Wrong Confidence" not in response.text
    assert 'name="min_profit" value="60.00"' in response.text
    assert '<option value="low" selected>' in response.text


@pytest.mark.parametrize(
    "query",
    [
        "unknown=value",
        "team=",
        "team=texans&team=aggies",
        "team=TEXANS",
        "event_id=0",
        "event_id=1.0",
        "source=private",
        "confidence=unknown",
        "status=unknown",
        "min_profit=1e2",
        "min_profit=NaN",
        "max_cost=Infinity",
        "max_cost=1234567890123.00",
        "max_cost=10.001",
    ],
)
def test_malformed_filters_fail_closed_without_echo(client, query):
    response = client.get(f"/?{query}")

    assert response.status_code in {400, 422}
    assert query not in response.text


def test_dashboard_html_has_local_security_headers(client):
    response = client.get("/")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_event_level_signal_never_renders_as_actionable_card(
    client, session_factory
):
    with session_factory() as session:
        _, observation, _ = seed_opportunity(
            session, profit=Decimal("999.00"), opponent="Evidence Only"
        )
        observation.kind = "event_floor"
        observation.can_buy_pair = None
        observation.quantity_available = None
        session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert "Evidence Only" not in response.text


def test_limit_is_applied_after_profit_ranking(client, session_factory):
    with session_factory() as session:
        seed_opportunity(
            session, profit=Decimal("999.00"), opponent="Highest Older Opportunity"
        )
        for index in range(200):
            seed_opportunity(
                session,
                profit=Decimal("1.00"),
                opponent=f"Low Opportunity {index}",
            )
        session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert "Highest Older Opportunity" in response.text
    assert response.text.count('class="opportunity-card') == 200


def test_equal_profit_uses_recent_then_id_tie_breakers(client, session_factory):
    with session_factory() as session:
        _, _, recent = seed_opportunity(
            session, profit=Decimal("50.00"), opponent="More Recent"
        )
        _, _, newer_id = seed_opportunity(
            session, profit=Decimal("50.00"), opponent="Newer ID"
        )
        recent.updated_at = NOW + timedelta(minutes=1)
        newer_id.updated_at = NOW
        session.commit()

    response = client.get("/")

    assert response.text.index("More Recent") < response.text.index("Newer ID")


def test_card_uses_persisted_listing_lineage_first_and_last_seen(
    client, session_factory
):
    with session_factory() as session:
        event, candidate, _ = seed_opportunity(
            session, profit=Decimal("50.00"), opponent="Lineage Opponent"
        )
        session.add(
            ObservationRow(
                event_id=event.id,
                source=candidate.source,
                event_external_id=candidate.event_external_id,
                observed_at=NOW - timedelta(days=1),
                kind="listing",
                currency="USD",
                pair_price=Decimal("210.00"),
                buyer_fees=Decimal("20.00"),
                estimated_tax=Decimal("5.00"),
                section="101",
                row="12",
                quantity_available=2,
                can_buy_pair=True,
                listing_id=candidate.listing_id,
                listing_identity=candidate.listing_identity,
                listing_url=candidate.listing_url,
                freshness_at=NOW - timedelta(days=1),
            )
        )
        session.commit()

    response = client.get("/")

    assert "First seen Aug 07, 2026 10:30 AM CDT" in response.text
    assert "Last seen Aug 08, 2026 10:30 AM CDT" in response.text
