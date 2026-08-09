from datetime import timedelta
from decimal import Decimal
import json
import re

from ticket_reviewer.data.schema import ObservationRow
from ticket_reviewer.web import routes

from .conftest import NOW, seed_opportunity


def test_event_detail_distinguishes_event_floor_from_pair_listing(
    client, session_factory
):
    with session_factory() as session:
        event, _, _ = seed_opportunity(session, profit=Decimal("82.00"))
        session.add(
            ObservationRow(
                event_id=event.id,
                source="ticketmaster",
                event_external_id="tm-event",
                observed_at=NOW,
                kind="event_floor",
                currency="USD",
                pair_price=Decimal("150.00"),
                buyer_fees=None,
                estimated_tax=None,
                section=None,
                row=None,
                quantity_available=None,
                can_buy_pair=None,
                listing_id=None,
                listing_identity="missing:event_floor",
                listing_url=None,
                freshness_at=NOW,
            )
        )
        event_id = event.id
        session.commit()

    response = client.get(f"/events/{event_id}")

    assert response.status_code == 200
    assert "event-level signal; pair not confirmed" in response.text
    assert "Price history table" in response.text
    assert 'type="application/json"' in response.text


def test_event_chart_json_cannot_be_broken_out_by_hostile_labels(
    client, session_factory
):
    hostile = "</script><script>window.stolen=true</script>"
    with session_factory() as session:
        event, observation, _ = seed_opportunity(
            session,
            profit=Decimal("82.00"),
            opponent=hostile,
            section=hostile,
            row="\x01 row",
        )
        event_id = event.id
        session.commit()

    response = client.get(f"/events/{event_id}")

    assert response.status_code == 200
    assert hostile not in response.text
    assert "<script>window.stolen" not in response.text
    payload = re.search(
        r'<script id="chart-data" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert payload is not None
    parsed = json.loads(payload.group(1))
    assert parsed["datasets"][0]["data"][0]["x"].endswith("Z")


def test_event_detail_null_price_is_unavailable_not_zero(client, session_factory):
    with session_factory() as session:
        event, observation, _ = seed_opportunity(
            session, profit=Decimal("82.00")
        )
        observation.pair_price = None
        event_id = event.id
        session.commit()

    response = client.get(f"/events/{event_id}")

    assert response.status_code == 200
    assert "Unavailable" in response.text
    assert '"y": "0"' not in response.text


def test_earlier_estimate_does_not_infer_missing_comparable_ids(
    client, session_factory
):
    with session_factory() as session:
        event, _, opportunity = seed_opportunity(
            session, profit=Decimal("82.00")
        )
        opportunity.scenarios = [
            {
                key: value
                for key, value in opportunity.scenarios[0].items()
                if key != "comparable_observation_ids"
            }
        ]
        event_id = event.id
        session.commit()

    response = client.get(f"/events/{event_id}")

    assert response.status_code == 200
    assert (
        "Exact comparable IDs were not recorded for this earlier estimate"
        in response.text
    )


def test_missing_event_has_safe_styled_not_found_page(client):
    response = client.get("/events/999999")

    assert response.status_code == 404
    assert "Event not found" in response.text
    assert "<!doctype html>" in response.text
    assert "traceback" not in response.text.lower()


def test_malformed_event_path_does_not_echo_raw_input(client):
    response = client.get("/events/private-malformed-value")

    assert response.status_code == 422
    assert "private-malformed-value" not in response.text
    assert "<!doctype html>" in response.text


def test_event_detail_bounds_history_by_retaining_newest_rows(
    client, session_factory, monkeypatch
):
    monkeypatch.setattr(routes, "_MAX_EVENT_OBSERVATIONS", 2)
    with session_factory() as session:
        event, observation, _ = seed_opportunity(session, profit=Decimal("82.00"))
        oldest = ObservationRow(
            event_id=event.id,
            source="stubhub",
            event_external_id=f"event-{event.id}",
            observed_at=NOW - timedelta(hours=2),
            kind="listing",
            currency="USD",
            pair_price=Decimal("111.00"),
            buyer_fees=None,
            estimated_tax=None,
            section="101",
            row="12",
            quantity_available=2,
            can_buy_pair=True,
            listing_id="oldest",
            listing_identity="listing:oldest",
            listing_url=None,
            freshness_at=NOW - timedelta(hours=2),
        )
        newest = ObservationRow(
            event_id=event.id,
            source="stubhub",
            event_external_id=f"event-{event.id}",
            observed_at=NOW + timedelta(hours=1),
            kind="listing",
            currency="USD",
            pair_price=Decimal("444.00"),
            buyer_fees=None,
            estimated_tax=None,
            section="101",
            row="12",
            quantity_available=2,
            can_buy_pair=True,
            listing_id="newest",
            listing_identity="listing:newest",
            listing_url=None,
            freshness_at=NOW + timedelta(hours=1),
        )
        session.add_all([oldest, newest])
        session.flush()
        event_id = event.id
        oldest_id = oldest.id
        newest_id = newest.id
        session.commit()

    response = client.get(f"/events/{event_id}")

    assert response.status_code == 200
    assert f">#{newest_id}<" in response.text
    assert f">#{oldest_id}<" not in response.text
    assert "newest 2 of 3 observations" in response.text


def test_event_detail_explains_estimate_math_confidence_and_risk(
    client, session_factory
):
    with session_factory() as session:
        event, _, _ = seed_opportunity(
            session,
            profit=Decimal("82.00"),
            confidence="low",
            risk_reasons=["limited seat-level comparables"],
        )
        event_id = event.id
        session.commit()

    response = client.get(f"/events/{event_id}")

    assert response.status_code == 200
    for expected in (
        "Acquisition total",
        "$225.00",
        "Projected resale gross",
        "$400.00",
        "Seller fee rate",
        "15.00%",
        "Seller fee amount",
        "$60.00",
        "Projected proceeds",
        "$340.00",
        "ROI",
        "Low confidence",
        "Risk and scoring adjustments",
        "limited seat-level comparables",
    ):
        assert expected in response.text
