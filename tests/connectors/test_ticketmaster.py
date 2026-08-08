import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from ticket_reviewer.bootstrap import build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.connectors.ticketmaster import TicketmasterConnector
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent


SEARCH_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
DETAIL_URL = (
    "https://app.ticketmaster.com/discovery/v2/events/texans-colts-home.json"
)
WINDOW_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 11, 1, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 8, 8, 16, 30, tzinfo=timezone.utc)
API_KEY = "sanitized-test-api-key"


@pytest.fixture
def fixture_json():
    path = Path(__file__).parents[1] / "fixtures" / "ticketmaster" / "texans_events.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def client():
    with httpx.Client() as value:
        yield value


@pytest.fixture
def connector(client):
    return TicketmasterConnector(
        Settings(_env_file=None, ticketmaster_api_key=API_KEY),
        client,
        sleep=lambda _delay: None,
        clock=lambda: OBSERVED_AT,
    )


@pytest.fixture
def tm_event():
    return ExternalEvent(
        source=Source.TICKETMASTER,
        external_id="texans-colts-home",
        team=Team.TEXANS,
        opponent="Colts",
        venue="NRG Stadium",
        starts_at=datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc),
        is_home=True,
        is_parking=False,
        url="https://www.ticketmaster.com/event/texans-colts-home",
    )


def test_connector_claims_only_public_event_capabilities():
    assert TicketmasterConnector.source is Source.TICKETMASTER
    assert TicketmasterConnector.capabilities == frozenset(
        {Capability.EVENT_SEARCH, Capability.EVENT_PRICE}
    )


@respx.mock
def test_discovers_only_texans_home_games(connector, fixture_json):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=fixture_json))

    events = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert [(event.opponent, event.venue) for event in events] == [
        ("Colts", "NRG Stadium")
    ]
    assert events[0] == ExternalEvent(
        source=Source.TICKETMASTER,
        external_id="texans-colts-home",
        team=Team.TEXANS,
        opponent="Colts",
        venue="NRG Stadium",
        starts_at=datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc),
        is_home=True,
        is_parking=False,
        url="https://www.ticketmaster.com/event/texans-colts-home",
    )


@respx.mock
def test_discovery_uses_exact_public_endpoint_and_normalized_parameters(
    connector, fixture_json
):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=fixture_json)
    )
    offset_start = WINDOW_START.astimezone(timezone(timedelta(hours=-5)))
    offset_end = WINDOW_END.astimezone(timezone(timedelta(hours=-5)))

    connector.discover(Team.TEXANS, offset_start, offset_end)

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.copy_with(query=None) == httpx.URL(SEARCH_URL)
    assert dict(request.url.params) == {
        "apikey": API_KEY,
        "keyword": "Houston Texans",
        "countryCode": "US",
        "startDateTime": "2026-09-01T00:00:00Z",
        "endDateTime": "2026-11-01T00:00:00Z",
        "size": "100",
    }


@respx.mock
def test_aggies_discovery_is_empty_without_network(connector):
    assert connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END) == []
    assert respx.calls.call_count == 0


@pytest.mark.parametrize(
    ("starts_after", "starts_before"),
    [
        (datetime(2026, 9, 1), WINDOW_END),
        (WINDOW_START, datetime(2026, 11, 1)),
        (WINDOW_START, WINDOW_START),
        (WINDOW_END, WINDOW_START),
    ],
)
def test_discovery_rejects_invalid_windows(connector, starts_after, starts_before):
    with pytest.raises(ValueError, match="time window"):
        connector.discover(Team.TEXANS, starts_after, starts_before)


@respx.mock
def test_discovery_reapplies_boundaries_deduplicates_and_sorts(
    connector, fixture_json
):
    home = fixture_json["_embedded"]["events"][0]
    at_start = copy.deepcopy(home)
    at_start["id"] = "at-start"
    at_start["name"] = "Houston Texans vs Jacksonville Jaguars"
    at_start["dates"]["start"]["dateTime"] = "2026-09-01T00:00:00Z"
    at_start["_embedded"]["attractions"][1]["name"] = "Jacksonville Jaguars"
    at_end = copy.deepcopy(home)
    at_end["id"] = "at-end"
    at_end["dates"]["start"]["dateTime"] = "2026-11-01T00:00:00Z"
    later = copy.deepcopy(home)
    later["id"] = "z-later"
    later["dates"]["start"]["dateTime"] = "2026-10-01T00:00:00Z"
    duplicate = copy.deepcopy(at_start)
    payload = {"_embedded": {"events": [later, at_end, at_start, duplicate]}}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))

    events = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert [event.external_id for event in events] == ["at-start", "z-later"]


@respx.mock
def test_discovery_preserves_fractional_utc_parameters_and_end_boundary(
    connector, fixture_json
):
    starts_after = datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
    starts_before = datetime(2026, 11, 1, 0, 0, 0, 654321, tzinfo=timezone.utc)
    home = fixture_json["_embedded"]["events"][0]
    just_before = copy.deepcopy(home)
    just_before["id"] = "just-before-fractional-end"
    just_before["dates"]["start"]["dateTime"] = "2026-11-01T00:00:00.654320Z"
    at_end = copy.deepcopy(home)
    at_end["id"] = "at-fractional-end"
    at_end["dates"]["start"]["dateTime"] = "2026-11-01T00:00:00.654321Z"
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"events": [at_end, just_before]}}
        )
    )

    events = connector.discover(Team.TEXANS, starts_after, starts_before)

    assert dict(route.calls[0].request.url.params)["startDateTime"] == (
        "2026-09-01T00:00:00.123456Z"
    )
    assert dict(route.calls[0].request.url.params)["endDateTime"] == (
        "2026-11-01T00:00:00.654321Z"
    )
    assert [event.external_id for event in events] == ["just-before-fractional-end"]


@respx.mock
def test_discovery_uses_chicago_local_date_fallback(connector, fixture_json):
    home = fixture_json["_embedded"]["events"][0]
    local_only = copy.deepcopy(home)
    local_only["id"] = "local-only"
    local_only["dates"]["start"].pop("dateTime")
    local_only["dates"]["start"]["localDate"] = "2026-09-14"
    local_only["dates"]["start"]["localTime"] = "19:15:00"
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"events": [local_only]}}
        )
    )

    events = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert events[0].starts_at == datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc)
    assert events[0].starts_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    ("mutation", "expected_ids"),
    [
        ("missing_texans_attraction_and_name", []),
        ("away_name", []),
        ("wrong_venue", []),
        ("parking_classification", []),
    ],
)
@respx.mock
def test_discovery_filters_required_home_event_signals(
    connector, fixture_json, mutation, expected_ids
):
    event = copy.deepcopy(fixture_json["_embedded"]["events"][0])
    if mutation == "missing_texans_attraction_and_name":
        event["name"] = "Indianapolis Colts Football"
        event["_embedded"]["attractions"] = [{"name": "Indianapolis Colts"}]
    elif mutation == "away_name":
        event["name"] = "Indianapolis Colts at Houston Texans"
    elif mutation == "wrong_venue":
        event["_embedded"]["venues"][0]["name"] = "Lucas Oil Stadium"
    else:
        event["classifications"][0]["subGenre"] = {"name": "Tailgate Pass"}
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [event]}})
    )

    assert [
        item.external_id
        for item in connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    ] == expected_ids


@pytest.mark.parametrize(
    ("location", "signal", "accepted"),
    [
        ("name", "Houston Texans vs Indianapolis Colts Tailgating", False),
        ("name", "Houston Texans vs Indianapolis Colts Passes", False),
        ("classification", "Tailgating", False),
        ("classification", "Passes", False),
        ("classification", "Passenger Experience", True),
        ("classification", "Compassion Sports", True),
    ],
)
@respx.mock
def test_parking_product_inflections_are_token_aware(
    connector, fixture_json, location, signal, accepted
):
    event = copy.deepcopy(fixture_json["_embedded"]["events"][0])
    if location == "name":
        event["name"] = signal
    else:
        event["classifications"][0]["subGenre"] = {"name": signal}
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [event]}})
    )

    events = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert bool(events) is accepted


@respx.mock
def test_opponent_falls_back_to_supported_home_name_and_accepts_reliant_alias(
    connector, fixture_json
):
    event = copy.deepcopy(fixture_json["_embedded"]["events"][0])
    event["id"] = "name-fallback"
    event["_embedded"]["attractions"] = []
    event["_embedded"]["venues"][0]["name"] = "Reliant Stadium"
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [event]}})
    )

    events = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert [(event.opponent, event.venue) for event in events] == [
        ("Colts", "Reliant Stadium")
    ]


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (
            "https://www.ticketmaster.com/event/public?apikey=url-secret#private",
            "https://www.ticketmaster.com/event/public",
        ),
        (
            "https://ticketmaster.com/event/public?tracking=private#fragment",
            "https://ticketmaster.com/event/public",
        ),
        ("http://www.ticketmaster.com/event/public", None),
        ("https://evil.example/event/public", None),
        ("https://ticketmaster.com.evil.example/event/public", None),
        ("https://attacker@www.ticketmaster.com/event/public", None),
    ],
)
@respx.mock
def test_discovery_exposes_only_allowlisted_sanitized_public_urls(
    connector, fixture_json, raw_url, expected_url
):
    event = copy.deepcopy(fixture_json["_embedded"]["events"][0])
    event["url"] = raw_url
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [event]}})
    )

    discovered = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert discovered[0].url == expected_url
    assert "url-secret" not in repr(discovered[0])
    assert "tracking=private" not in repr(discovered[0])


@respx.mock
def test_detail_and_fallback_urls_use_the_same_public_url_boundary(
    connector, tm_event
):
    event = ExternalEvent(
        source=tm_event.source,
        external_id=tm_event.external_id,
        team=tm_event.team,
        opponent=tm_event.opponent,
        venue=tm_event.venue,
        starts_at=tm_event.starts_at,
        is_home=tm_event.is_home,
        is_parking=tm_event.is_parking,
        url="https://ticketmaster.com/event/fallback?token=fallback-secret#private",
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://evil.example/event/detail?token=detail-secret",
                "priceRanges": [{"currency": "USD", "min": 50}],
            },
        )
    )

    observation = connector.fetch_observations(event)[0]

    assert observation.listing_url == "https://ticketmaster.com/event/fallback"
    assert "fallback-secret" not in repr(observation)
    assert "detail-secret" not in repr(observation)


@respx.mock
def test_public_price_range_is_pair_normalized_event_evidence(
    connector, tm_event, fixture_json
):
    detail = fixture_json["_embedded"]["events"][0]
    route = respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=detail))

    observations = connector.fetch_observations(tm_event)

    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {"apikey": API_KEY}
    assert len(observations) == 1
    observation = observations[0]
    assert observation.source is Source.TICKETMASTER
    assert observation.event_external_id == "texans-colts-home"
    assert observation.observed_at == OBSERVED_AT
    assert observation.kind is ObservationKind.EVENT_FLOOR
    assert observation.currency == "USD"
    assert observation.pair_price == Decimal("165.00")
    assert observation.buyer_fees is None
    assert observation.estimated_tax is None
    assert observation.section is None
    assert observation.row is None
    assert observation.quantity_available is None
    assert observation.can_buy_pair is None
    assert observation.listing_id is None
    assert observation.listing_url == detail["url"]


@respx.mock
def test_price_parser_chooses_lowest_valid_usd_minimum(connector, tm_event):
    payload = {
        "url": "https://www.ticketmaster.com/event/texans-colts-home",
        "priceRanges": [
            {"currency": "EUR", "min": 1},
            {"currency": "USD", "min": 0},
            {"currency": "USD", "min": -1},
            {"currency": "USD", "min": "NaN"},
            {"currency": "USD", "min": "Infinity"},
            {"currency": "USD", "min": "bad"},
            {"currency": "USD", "min": "91.005"},
            {"currency": "USD", "min": "80.004"},
        ],
    }
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))

    observations = connector.fetch_observations(tm_event)

    assert observations[0].pair_price == Decimal("160.01")


@pytest.mark.parametrize(
    "price_ranges",
    [
        None,
        [],
        [{"currency": "EUR", "min": 1}],
        [{"currency": "USD", "min": "0.001"}],
        [{"currency": "USD", "min": "1E+999999"}],
    ],
)
@respx.mock
def test_missing_or_unusable_price_ranges_return_no_observation(
    connector, tm_event, price_ranges
):
    payload = {"url": tm_event.url}
    if price_ranges is not None:
        payload["priceRanges"] = price_ranges
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))

    assert connector.fetch_observations(tm_event) == []


def test_fetch_observations_rejects_wrong_source(connector, tm_event):
    wrong_source = ExternalEvent(
        source=Source.STUBHUB,
        external_id=tm_event.external_id,
        team=tm_event.team,
        opponent=tm_event.opponent,
        venue=tm_event.venue,
        starts_at=tm_event.starts_at,
        is_home=True,
        is_parking=False,
        url=tm_event.url,
    )
    with pytest.raises(ValueError, match="Ticketmaster"):
        connector.fetch_observations(wrong_source)


@pytest.mark.parametrize(
    "external_id",
    [None, 123, "", "   ", "attacker-secret-" + ("x" * 257), "\ud800attacker-secret"],
)
@respx.mock
def test_invalid_external_ids_fail_generically_without_network_or_leakage(
    connector, tm_event, external_id
):
    event = ExternalEvent(
        source=Source.TICKETMASTER,
        external_id=external_id,
        team=tm_event.team,
        opponent=tm_event.opponent,
        venue=tm_event.venue,
        starts_at=tm_event.starts_at,
        is_home=True,
        is_parking=False,
        url=tm_event.url,
    )

    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(event)

    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False
    exposed = f"{caught.value!s} {caught.value!r} {caught.value.args!r}"
    assert "attacker-secret" not in exposed
    assert respx.calls.call_count == 0


@respx.mock
def test_external_id_is_bounded_then_safely_encoded_as_one_path_segment(
    connector, tm_event
):
    external_id = "abc/def ?é"
    event = ExternalEvent(
        source=Source.TICKETMASTER,
        external_id=external_id,
        team=tm_event.team,
        opponent=tm_event.opponent,
        venue=tm_event.venue,
        starts_at=tm_event.starts_at,
        is_home=True,
        is_parking=False,
        url=tm_event.url,
    )
    route = respx.get(
        "https://app.ticketmaster.com/discovery/v2/events/abc%2Fdef%20%3F%C3%A9.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "url": tm_event.url,
                "priceRanges": [{"currency": "USD", "min": 50}],
            },
        )
    )

    observation = connector.fetch_observations(event)[0]

    assert route.call_count == 1
    assert observation.event_external_id == external_id


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
def test_http_statuses_map_safely_and_retry_only_transient_failures(
    client, status, category, retryable, calls
):
    secret = "status-secret-key"
    sleeps = []
    connector = TicketmasterConnector(
        Settings(_env_file=None, ticketmaster_api_key=secret),
        client,
        sleep=sleeps.append,
        clock=lambda: OBSERVED_AT,
    )
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(status, text="private response body Bearer private")
    )

    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    failure = caught.value
    assert failure.category is category
    assert failure.retryable is retryable
    assert route.call_count == calls
    assert len(sleeps) == calls - 1
    exposed = f"{failure!s} {failure!r} {failure.args!r}"
    assert secret not in exposed
    assert "private response body" not in exposed
    assert "apikey" not in exposed


@respx.mock
def test_network_failures_retry_and_never_expose_exception_text(client):
    secret = "network-secret-key"
    request = httpx.Request("GET", f"{SEARCH_URL}?apikey={secret}")
    route = respx.get(SEARCH_URL).mock(
        side_effect=httpx.ConnectError(
            f"network failed with {secret} and private headers", request=request
        )
    )
    connector = TicketmasterConnector(
        Settings(_env_file=None, ticketmaster_api_key=secret),
        client,
        sleep=lambda _delay: None,
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert caught.value.category is FailureCategory.NETWORK
    assert caught.value.retryable is True
    assert route.call_count == 3
    assert secret not in f"{caught.value!s} {caught.value!r}"
    assert "private headers" not in f"{caught.value!s} {caught.value!r}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"_embedded": {"events": {}}}),
    ],
)
@respx.mock
def test_malformed_discovery_payload_is_safe_nonretryable_parse(
    connector, response
):
    route = respx.get(SEARCH_URL).mock(return_value=response)

    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)

    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False
    assert route.call_count == 1
    assert "not-json" not in f"{caught.value!s} {caught.value!r}"


@respx.mock
def test_malformed_event_detail_payload_is_safe_parse(connector, tm_event):
    route = respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(tm_event)

    assert caught.value.category is FailureCategory.PARSE
    assert caught.value.retryable is False
    assert route.call_count == 1


def test_settings_bound_ticketmaster_timeout():
    assert Settings(_env_file=None).ticketmaster_http_timeout_seconds == 10.0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ticketmaster_http_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ticketmaster_http_timeout_seconds=121)


def test_bootstrap_conditionally_registers_and_closes_ticketmaster_client():
    secret = "bootstrap-secret-key"
    services = build_services(
        Settings(_env_file=None, ticketmaster_api_key=secret),
        session_factory=lambda: None,
    )

    assert [connector.source for connector in services.connectors] == [
        Source.TICKETMASTER
    ]
    connector = services.connectors[0]
    assert secret not in repr(services)
    assert secret not in repr(connector)
    assert connector.client.is_closed is False

    services.close()
    services.close()

    assert connector.client.is_closed is True


def test_bootstrap_preserves_injected_ticketmaster_and_does_not_close_it():
    class InjectedTicketmaster:
        source = Source.TICKETMASTER
        capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})

        def __init__(self):
            self.close_calls = 0

        def discover(self, team, starts_after, starts_before):
            return []

        def fetch_observations(self, event):
            return []

        def close(self):
            self.close_calls += 1

    injected = InjectedTicketmaster()
    services = build_services(
        Settings(_env_file=None, ticketmaster_api_key=API_KEY),
        connectors=(injected,),
        session_factory=lambda: None,
    )

    assert services.connectors == (injected,)
    services.close()
    assert injected.close_calls == 0


def test_bootstrap_does_not_register_for_missing_or_blank_key():
    for key in (None, "", "   "):
        services = build_services(
            Settings(_env_file=None, ticketmaster_api_key=key),
            session_factory=lambda: None,
        )
        assert services.connectors == ()
        services.close()


def test_bootstrap_closes_owned_client_when_service_composition_fails(monkeypatch):
    class ClientDouble:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    client_double = ClientDouble()
    monkeypatch.setattr(
        "ticket_reviewer.bootstrap.httpx.Client",
        lambda **_kwargs: client_double,
    )
    monkeypatch.setattr(
        "ticket_reviewer.bootstrap.ScanCoordinator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("sanitized scanner construction failure")
        ),
    )

    with pytest.raises(ValueError, match="scanner construction failure"):
        build_services(
            Settings(_env_file=None, ticketmaster_api_key=API_KEY),
            session_factory=lambda: None,
        )

    assert client_double.close_calls == 1


def test_connector_close_does_not_close_injected_client(client, connector):
    connector.close()
    connector.close()
    assert client.is_closed is False
