import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import select

from tests.factories import make_event
from ticket_reviewer.bootstrap import build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.connectors.seatgeek import SeatGeekConnector
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.data.schema import Base, ObservationRow, OpportunityRow
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent
from ticket_reviewer.services.scanner import RepositoryBundle, ScanCoordinator


SEARCH_URL = "https://api.seatgeek.com/2/events"
DETAIL_URL = "https://api.seatgeek.com/2/events/91001"
WINDOW_START = datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 11, 1, 0, 0, 0, 654321, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 8, 8, 16, 30, 0, 432100, tzinfo=timezone.utc)
CLIENT_ID = "sanitized-client-id"
CLIENT_SECRET = "sanitized-client-secret"


@pytest.fixture
def fixture_json():
    path = Path(__file__).parents[1] / "fixtures" / "seatgeek" / "aggies_events.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def client():
    with httpx.Client() as value:
        yield value


@pytest.fixture
def connector(client):
    return SeatGeekConnector(
        Settings(
            _env_file=None,
            seatgeek_client_id=CLIENT_ID,
            seatgeek_client_secret=CLIENT_SECRET,
        ),
        client,
        sleep=lambda _delay: None,
        clock=lambda: OBSERVED_AT,
    )


@pytest.fixture
def sg_event():
    return ExternalEvent(
        source=Source.SEATGEEK,
        external_id="91001",
        team=Team.AGGIES,
        opponent="LSU Tigers Football",
        venue="Kyle Field",
        starts_at=datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc),
        is_home=True,
        is_parking=False,
        url="https://seatgeek.com/lsu-tigers-at-texas-a-m-aggies-football-tickets/college-station-texas-kyle-field-2026-10-17-6-30-pm/91001",
    )


def detail_payload(fixture_json):
    return copy.deepcopy(fixture_json["events"][0])


def test_claims_only_official_event_capabilities():
    assert SeatGeekConnector.source is Source.SEATGEEK
    assert SeatGeekConnector.capabilities == frozenset(
        {Capability.EVENT_SEARCH, Capability.EVENT_PRICE}
    )


@respx.mock
def test_search_uses_exact_official_endpoint_auth_and_fractional_utc_params(
    connector, fixture_json
):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=fixture_json))
    offset = timezone(timedelta(hours=-5))

    connector.discover(
        Team.AGGIES, WINDOW_START.astimezone(offset), WINDOW_END.astimezone(offset)
    )

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.copy_with(query=None) == httpx.URL(SEARCH_URL)
    assert dict(request.url.params) == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "performers.slug": "texas-a-m-aggies-football",
        "datetime_utc.gte": "2026-09-01T00:00:00.123456Z",
        "datetime_utc.lte": "2026-11-01T00:00:00.654321Z",
        "venue.state": "TX",
        "per_page": "100",
    }


@respx.mock
def test_maps_only_aggies_home_game_at_kyle(connector, fixture_json):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=fixture_json))

    events = connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    assert events == [
        ExternalEvent(
            source=Source.SEATGEEK,
            external_id="91001",
            team=Team.AGGIES,
            opponent="LSU Tigers Football",
            venue="Kyle Field",
            starts_at=datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc),
            is_home=True,
            is_parking=False,
            url=fixture_json["events"][0]["url"],
        )
    ]


@respx.mock
def test_discovery_normalizes_kyle_name_and_rejects_naive_event_time(
    connector, fixture_json
):
    event = detail_payload(fixture_json)
    event["venue"]["name"] = "  KYLE---FIELD  "
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"events": [event]})
    )

    assert connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)[0].venue == (
        "Kyle Field"
    )

    event["datetime_utc"] = "2026-10-17T23:30:00"
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"events": [event]})
    )
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)
    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False


@respx.mock
def test_texans_scope_returns_empty_without_network(connector):
    assert connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END) == []
    assert respx.calls.call_count == 0


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 1), WINDOW_END),
        (WINDOW_START, datetime(2026, 11, 1)),
        (WINDOW_START, WINDOW_START),
        (WINDOW_END, WINDOW_START),
    ],
)
def test_rejects_naive_or_unordered_windows(connector, start, end):
    with pytest.raises(ValueError, match="time window"):
        connector.discover(Team.AGGIES, start, end)


@pytest.mark.parametrize(
    "mutation",
    [
        "away_title",
        "neutral_venue",
        "non_texas_venue",
        "parking_title",
        "tailgate_performer",
        "pass_venue",
        "unrelated_performers",
    ],
)
@respx.mock
def test_rejects_away_neutral_parking_and_unrelated_events(
    connector, fixture_json, mutation
):
    event = detail_payload(fixture_json)
    if mutation == "away_title":
        event["title"] = "Texas A&M Aggies Football at LSU Tigers Football"
    elif mutation == "neutral_venue":
        event["venue"]["name"] = "AT&T Stadium"
    elif mutation == "non_texas_venue":
        event["venue"]["state"] = "LA"
    elif mutation == "parking_title":
        event["title"] += " Parking"
    elif mutation == "tailgate_performer":
        event["performers"][0]["name"] += " Tailgating"
    elif mutation == "pass_venue":
        event["venue"]["name"] = "Kyle Field Passes"
    else:
        event["title"] = "LSU Football at Kyle Field"
        event["performers"] = [{"name": "LSU Tigers", "slug": "lsu-tigers"}]
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"events": [event]})
    )

    assert connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END) == []


@respx.mock
def test_accepts_conservative_aggies_home_vs_and_dedupes_sorts(connector, fixture_json):
    base = detail_payload(fixture_json)
    later = copy.deepcopy(base)
    later.update(id=91003, title="Texas A&M Aggies Football vs Auburn Tigers Football")
    later["datetime_utc"] = "2026-10-20T01:00:00Z"
    later["performers"][1] = {"name": "Auburn Tigers Football", "slug": "auburn-tigers-football"}
    earlier = copy.deepcopy(base)
    earlier.update(id=91002, title="Alabama Crimson Tide at Texas A&M Aggies Football")
    earlier["datetime_utc"] = "2026-09-20T01:00:00Z"
    earlier["performers"][1] = {"name": "Alabama Crimson Tide", "slug": "alabama-crimson-tide"}
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"events": [later, base, earlier, earlier]})
    )

    events = connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    assert [(event.external_id, event.opponent) for event in events] == [
        ("91002", "Alabama Crimson Tide"),
        ("91001", "LSU Tigers Football"),
        ("91003", "Auburn Tigers Football"),
    ]


@respx.mock
def test_opponent_fallback_preserves_public_title_acronyms(connector, fixture_json):
    event = detail_payload(fixture_json)
    event["title"] = "LSU Tigers at Texas A&M Aggies Football"
    event["performers"] = [
        {"name": "Texas A&M Aggies Football", "slug": "texas-a-m-aggies-football"}
    ]
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"events": [event]})
    )

    discovered = connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    assert discovered[0].opponent == "LSU Tigers"


@respx.mock
def test_blank_optional_secret_is_omitted_from_auth_query(client, fixture_json):
    connector = SeatGeekConnector(
        Settings(
            _env_file=None,
            seatgeek_client_id=CLIENT_ID,
            seatgeek_client_secret="   ",
        ),
        client,
        sleep=lambda _delay: None,
    )
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=fixture_json)
    )

    connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    params = dict(route.calls[0].request.url.params)
    assert params["client_id"] == CLIENT_ID
    assert "client_secret" not in params


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("https://seatgeek.com/events/91001?client_id=secret#frag", "https://seatgeek.com/events/91001"),
        ("https://www.seatgeek.com/events/91001?tracking=x", "https://www.seatgeek.com/events/91001"),
        ("http://seatgeek.com/events/91001", None),
        ("https://evil.example/events/91001", None),
        ("https://seatgeek.com.evil.example/events/91001", None),
        ("https://attacker@seatgeek.com/events/91001", None),
        ("https://seatgeek.com:444/events/91001", None),
    ],
)
@respx.mock
def test_public_url_is_allowlisted_and_sanitized(connector, fixture_json, raw_url, expected):
    event = detail_payload(fixture_json)
    event["url"] = raw_url
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"events": [event]}))

    discovered = connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)[0]

    assert discovered.url == expected


@pytest.mark.parametrize("value", [None, "", "  ", "-1", "12/34", "1" * 30, "attacker-secret"])
@respx.mock
def test_invalid_numeric_event_id_is_safe_parse_without_network(connector, sg_event, value):
    event = replace(sg_event, external_id=value)

    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(event)

    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False
    assert "attacker-secret" not in f"{caught.value!s} {caught.value!r}"
    assert respx.calls.call_count == 0


@respx.mock
def test_maps_aggies_event_aggregates(connector, fixture_json, sg_event):
    route = respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, json=detail_payload(fixture_json))
    )

    observations = connector.fetch_observations(sg_event)

    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    floor = next(item for item in observations if item.kind is ObservationKind.EVENT_FLOOR)
    aggregate = next(item for item in observations if item.kind is ObservationKind.EVENT_AGGREGATE)
    assert floor.pair_price == Decimal("240.00")
    assert aggregate.pair_price == Decimal("350.50")
    assert aggregate.listing_count == 850
    assert aggregate.popularity == Decimal("0.875")
    assert floor.can_buy_pair is None
    assert {item.observed_at for item in observations} == {OBSERVED_AT}
    for item in observations:
        assert item.quantity_available is None
        assert item.can_buy_pair is None
        assert item.listing_id is None
        assert item.section is None
        assert item.row is None


@respx.mock
def test_detail_revalidates_same_aggies_home_kyle_event(connector, fixture_json, sg_event):
    payload = detail_payload(fixture_json)
    payload["venue"]["name"] = "Tiger Stadium"
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(sg_event)

    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("stats", "score", "expected"),
    [
        ({"average_price": "10.005"}, None, [(ObservationKind.EVENT_AGGREGATE, Decimal("20.01"), None, None)]),
        ({"highest_price": "12.345"}, None, [(ObservationKind.EVENT_AGGREGATE, Decimal("24.69"), None, None)]),
        ({"listing_count": "12.0"}, None, [(ObservationKind.EVENT_AGGREGATE, None, 12, None)]),
        ({}, "0.25", [(ObservationKind.EVENT_AGGREGATE, None, None, Decimal("0.25"))]),
        ({"lowest_price": "0.001", "average_price": "0.001"}, None, []),
        ({}, None, []),
    ],
)
@respx.mock
def test_partial_stats_and_pair_basis_fallbacks(
    connector, fixture_json, sg_event, stats, score, expected
):
    payload = detail_payload(fixture_json)
    payload["stats"] = stats
    if score is None:
        payload.pop("score", None)
    else:
        payload["score"] = score
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))

    observations = connector.fetch_observations(sg_event)

    assert [
        (item.kind, item.pair_price, item.listing_count, item.popularity)
        for item in observations
    ] == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lowest_price", "-0.01"),
        ("average_price", "NaN"),
        ("highest_price", "Infinity"),
        ("highest_price", "1E+999999"),
        ("listing_count", True),
        ("listing_count", "12.5"),
        ("listing_count", -1),
        ("score", "NaN"),
        ("score", "1.0001"),
        ("score", -1),
    ],
)
@respx.mock
def test_invalid_stats_are_safe_nonretryable_parse(
    connector, fixture_json, sg_event, field, value
):
    payload = detail_payload(fixture_json)
    if field == "score":
        payload[field] = value
    else:
        payload["stats"][field] = value
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(sg_event)

    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("status", "category", "retryable", "calls"),
    [
        (401, FailureCategory.AUTH, False, 1),
        (403, FailureCategory.AUTH, False, 1),
        (429, FailureCategory.RATE_LIMIT, True, 3),
        (500, FailureCategory.NETWORK, True, 3),
        (400, FailureCategory.PARSE, False, 1),
    ],
)
@respx.mock
def test_http_failures_retry_safely(client, status, category, retryable, calls):
    sleeps = []
    connector = SeatGeekConnector(
        Settings(_env_file=None, seatgeek_client_id=CLIENT_ID, seatgeek_client_secret=CLIENT_SECRET),
        client,
        sleep=sleeps.append,
    )
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(status, text="private body client_secret=leak")
    )

    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    assert (caught.value.category, caught.value.retryable) == (category, retryable)
    assert route.call_count == calls
    assert len(sleeps) == calls - 1
    exposed = f"{caught.value!s} {caught.value!r} {caught.value.args!r}"
    assert CLIENT_ID not in exposed
    assert CLIENT_SECRET not in exposed
    assert "private body" not in exposed


@respx.mock
def test_network_failure_retries_without_raw_exception(client):
    request = httpx.Request("GET", f"{SEARCH_URL}?client_secret={CLIENT_SECRET}")
    route = respx.get(SEARCH_URL).mock(
        side_effect=httpx.TimeoutException(
            f"timeout {CLIENT_SECRET} private headers", request=request
        )
    )
    connector = SeatGeekConnector(
        Settings(_env_file=None, seatgeek_client_id=CLIENT_ID, seatgeek_client_secret=CLIENT_SECRET),
        client,
        sleep=lambda _delay: None,
    )

    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    assert caught.value.category is FailureCategory.NETWORK
    assert caught.value.retryable is True
    assert route.call_count == 3
    assert CLIENT_SECRET not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json private"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"events": {}}),
    ],
)
@respx.mock
def test_malformed_search_payload_is_safe_parse(connector, response):
    route = respx.get(SEARCH_URL).mock(return_value=response)
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)
    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False
    assert route.call_count == 1
    assert "private" not in f"{caught.value!s} {caught.value!r}"


def test_settings_bound_seatgeek_timeout_and_keep_blank_credentials():
    settings = Settings(
        _env_file=None, seatgeek_client_id="", seatgeek_client_secret=""
    )
    assert settings.seatgeek_http_timeout_seconds == 10.0
    assert settings.seatgeek_client_id.get_secret_value() == ""
    assert settings.seatgeek_client_secret.get_secret_value() == ""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seatgeek_http_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, seatgeek_http_timeout_seconds=121)


def test_bootstrap_conditionally_registers_owned_seatgeek_and_preserves_injected():
    services = build_services(
        Settings(_env_file=None, seatgeek_client_id=CLIENT_ID),
        session_factory=lambda: None,
    )
    assert [item.source for item in services.connectors] == [Source.SEATGEEK]
    owned = services.connectors[0]
    assert CLIENT_ID not in repr(services)
    assert CLIENT_ID not in repr(owned)
    assert owned.client.is_closed is False
    services.close()
    services.close()
    assert owned.client.is_closed is True

    class Injected:
        source = Source.SEATGEEK
        capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})

        def discover(self, *_args):
            return []

        def fetch_observations(self, _event):
            return []

        def close(self):
            raise AssertionError("injected connector must not be closed")

    injected = Injected()
    services = build_services(
        Settings(_env_file=None, seatgeek_client_id=CLIENT_ID),
        connectors=(injected,),
        session_factory=lambda: None,
    )
    assert services.connectors == (injected,)
    services.close()


def test_bootstrap_closes_all_created_clients_when_scanner_construction_fails(monkeypatch):
    clients = []

    class ClientDouble:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    def client_factory(**_kwargs):
        value = ClientDouble()
        clients.append(value)
        return value

    class Injected:
        source = Source.STUBHUB
        capabilities = frozenset({Capability.EVENT_SEARCH})

        def discover(self, *_args):
            return []

        def fetch_observations(self, _event):
            return []

    monkeypatch.setattr("ticket_reviewer.bootstrap.httpx.Client", client_factory)

    with pytest.raises(ValueError, match="duplicate connector source"):
        build_services(
            Settings(
                _env_file=None,
                ticketmaster_api_key="ticketmaster-key",
                seatgeek_client_id=CLIENT_ID,
            ),
            connectors=(Injected(), Injected()),
            session_factory=lambda: None,
        )

    assert len(clients) == 2
    assert [client.close_calls for client in clients] == [1, 1]


def test_bootstrap_closes_first_owned_client_when_second_client_construction_fails(
    monkeypatch,
):
    class ClientDouble:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    first = ClientDouble()
    calls = 0

    def client_factory(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise RuntimeError("sanitized construction failure")

    monkeypatch.setattr("ticket_reviewer.bootstrap.httpx.Client", client_factory)

    with pytest.raises(RuntimeError, match="sanitized construction failure"):
        build_services(
            Settings(
                _env_file=None,
                ticketmaster_api_key="ticketmaster-key",
                seatgeek_client_id=CLIENT_ID,
            ),
            session_factory=lambda: None,
        )

    assert first.close_calls == 1


@respx.mock
def test_floor_and_aggregate_persist_once_without_becoming_listings(
    tmp_path, connector, fixture_json, sg_event
):
    engine, session_factory = create_engine_and_session(
        f"sqlite:///{tmp_path / 'seatgeek.db'}"
    )
    Base.metadata.create_all(engine)
    payload = detail_payload(fixture_json)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"events": [payload]}))
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    scanner = ScanCoordinator(
        Settings(_env_file=None),
        session_factory,
        RepositoryBundle,
        (connector,),
        clock=lambda: OBSERVED_AT,
    )

    first = scanner.run(OBSERVED_AT)
    second = scanner.run(OBSERVED_AT)

    assert (first.observations_saved, first.opportunities_saved) == (2, 0)
    assert (second.observations_saved, second.opportunities_saved) == (0, 0)
    with session_factory() as session:
        rows = session.scalars(select(ObservationRow).order_by(ObservationRow.kind)).all()
        assert [row.listing_identity for row in rows] == [
            "missing:event_aggregate",
            "missing:event_floor",
        ]
        assert all(row.can_buy_pair is None and row.quantity_available is None for row in rows)
        assert session.scalars(select(OpportunityRow)).all() == []
    engine.dispose()
