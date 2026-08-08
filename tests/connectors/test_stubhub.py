import base64
import copy
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import ValidationError

from ticket_reviewer.bootstrap import build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.connectors.stubhub import StubHubConnector, StubHubTokenProvider
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent


TOKEN_URL = "https://account.stubhub.com/oauth2/token"
SEARCH_URL = "https://api.stubhub.net/catalog/events/search"
DETAIL_URL = "https://api.stubhub.net/catalog/events/120001"
CLIENT_ID = "sanitized-client-id"
CLIENT_SECRET = "sanitized-client-secret"
NOW = datetime(2026, 8, 8, 17, 0, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 11, 1, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 8, 8, 17, 30, tzinfo=timezone.utc)


@pytest.fixture
def token_json():
    path = Path(__file__).parents[1] / "fixtures" / "stubhub" / "token.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def search_json():
    path = Path(__file__).parents[1] / "fixtures" / "stubhub" / "texans_search.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def client():
    with httpx.Client() as value:
        yield value


def settings(**overrides):
    return Settings(
        _env_file=None,
        stubhub_client_id=CLIENT_ID,
        stubhub_client_secret=CLIENT_SECRET,
        **overrides,
    )


@pytest.fixture
def token_provider(client):
    return StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)


@pytest.fixture
def connector(client):
    return StubHubConnector(
        settings(),
        client,
        sleep=lambda _delay: None,
        clock=lambda: OBSERVED_AT,
    )


@pytest.fixture
def stubhub_event():
    return ExternalEvent(
        source=Source.STUBHUB,
        external_id="120001",
        team=Team.TEXANS,
        opponent="Baltimore Ravens",
        venue="NRG Stadium",
        starts_at=datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc),
        is_home=True,
        is_parking=False,
        url="https://www.stubhub.com/houston-texans-tickets/event/120001",
    )


def detail(search_json, index=0):
    return copy.deepcopy(search_json["_embedded"]["events"][index])


def authorize(token_json, token="access-token"):
    payload = copy.deepcopy(token_json)
    payload["access_token"] = token
    return respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=payload))


def test_claims_catalog_event_capabilities_and_nonactionable_health_note():
    assert StubHubConnector.source is Source.STUBHUB
    assert StubHubConnector.capabilities == frozenset(
        {Capability.EVENT_SEARCH, Capability.EVENT_PRICE}
    )
    assert Capability.LISTING_DETAIL not in StubHubConnector.capabilities
    assert StubHubConnector.health_note == (
        "Detailed StubHub buyer inventory is unavailable to this key; "
        "event floors cannot trigger actionable alerts."
    )


@respx.mock
def test_oauth_uses_exact_basic_form_and_scope(token_provider, token_json):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_json))

    assert token_provider.get_token(NOW) == "access-token"

    request = route.calls[0].request
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "scope": ["read:events"],
    }
    assert CLIENT_ID not in request.content.decode()
    assert CLIENT_SECRET not in request.content.decode()
    assert request.url == httpx.URL(TOKEN_URL)


@respx.mock
def test_oauth_preserves_exact_nonblank_unicode_credentials(client, token_json):
    exact_id = "  clïent-id  "
    exact_secret = " sëcret value "
    provider = StubHubTokenProvider(
        Settings(
            _env_file=None,
            stubhub_client_id=exact_id,
            stubhub_client_secret=exact_secret,
        ),
        client,
        sleep=lambda _delay: None,
    )
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_json)
    )

    assert provider.get_token(NOW) == "access-token"

    expected = base64.b64encode(f"{exact_id}:{exact_secret}".encode("utf-8")).decode()
    assert route.calls[0].request.headers["authorization"] == f"Basic {expected}"


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [
        ("   ", CLIENT_SECRET),
        (CLIENT_ID, "\t"),
        ("bad\x00id", CLIENT_SECRET),
        (CLIENT_ID, "bad\nsecret"),
        ("bad\ud800id", CLIENT_SECRET),
        ("x" * 1025, CLIENT_SECRET),
        (CLIENT_ID, "x" * 1025),
    ],
)
@respx.mock
def test_oauth_rejects_blank_unsafe_or_oversized_credentials_before_request(
    client, client_id, client_secret
):
    with pytest.raises(ValueError) as caught:
        StubHubTokenProvider(
            Settings(
                _env_file=None,
                stubhub_client_id=client_id,
                stubhub_client_secret=client_secret,
            ),
            client,
            sleep=lambda _delay: None,
        )
    exposed = f"{caught.value!s} {caught.value!r} {caught.value.args!r}"
    for credential in (client_id, client_secret):
        if credential.strip():
            assert credential not in exposed
    assert respx.calls.call_count == 0


def test_oauth_maps_basic_auth_construction_exception_to_safe_auth(
    client, token_json, monkeypatch
):
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)

    def fail_basic_auth(*_args, **_kwargs):
        raise RuntimeError(f"construction failed {CLIENT_SECRET}")

    monkeypatch.setattr(
        "ticket_reviewer.connectors.stubhub.httpx.BasicAuth", fail_basic_auth
    )

    with pytest.raises(ConnectorFailure) as caught:
        provider.get_token(NOW)

    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.AUTH,
        False,
    )
    exposed = f"{caught.value!s} {caught.value!r} {caught.value.args!r}"
    assert CLIENT_ID not in exposed
    assert CLIENT_SECRET not in exposed


@respx.mock
def test_reuses_token_until_safety_window(token_provider, token_json):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_json))
    first = token_provider.get_token(NOW)
    second = token_provider.get_token(NOW + timedelta(minutes=5))
    assert first == second == "access-token"
    assert route.call_count == 1


@respx.mock
def test_refreshes_at_safety_window_and_does_not_reuse_short_lifetime(
    token_provider, token_json
):
    responses = []
    for token, lifetime in (("one", 120), ("two", 60), ("three", 3600)):
        payload = copy.deepcopy(token_json)
        payload.update(access_token=token, expires_in=lifetime)
        responses.append(httpx.Response(200, json=payload))
    route = respx.post(TOKEN_URL).mock(side_effect=responses)

    assert token_provider.get_token(NOW) == "one"
    assert token_provider.get_token(NOW + timedelta(seconds=59)) == "one"
    assert token_provider.get_token(NOW + timedelta(seconds=60)) == "two"
    assert token_provider.get_token(NOW + timedelta(seconds=61)) == "three"
    assert route.call_count == 3


@pytest.mark.parametrize(
    ("issued_at", "expires_in"),
    [
        (datetime.max.replace(tzinfo=timezone.utc), 3600),
        (datetime.min.replace(tzinfo=timezone.utc), 1),
    ],
)
@respx.mock
def test_unrepresentable_cache_deadline_returns_token_without_caching(
    client, token_json, issued_at, expires_in
):
    post_count = 0

    def response(_request):
        nonlocal post_count
        post_count += 1
        payload = copy.deepcopy(token_json)
        payload.update(access_token=f"token-{post_count}", expires_in=expires_in)
        return httpx.Response(200, json=payload)

    route = respx.post(TOKEN_URL).mock(side_effect=response)
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)

    assert provider.get_token(issued_at) == "token-1"
    assert provider.get_token(issued_at) == "token-2"
    assert route.call_count == 2


def test_token_provider_requires_aware_now(token_provider):
    with pytest.raises(ValueError, match="timezone-aware"):
        token_provider.get_token(NOW.replace(tzinfo=None))


@respx.mock
def test_token_cache_is_concurrency_safe(client, token_json):
    calls = 0
    calls_lock = threading.Lock()

    def response(_request):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return httpx.Response(200, json=token_json)

    respx.post(TOKEN_URL).mock(side_effect=response)
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(provider.get_token, [NOW] * 8))

    assert tokens == ["access-token"] * 8
    assert calls == 1


@respx.mock
def test_waiter_two_hours_ahead_refreshes_instead_of_using_leader_token(
    client, token_json
):
    first_request_started = threading.Event()
    waiter_ready = threading.Event()
    post_count = 0
    post_lock = threading.Lock()

    def response(_request):
        nonlocal post_count
        with post_lock:
            post_count += 1
            current = post_count
        payload = copy.deepcopy(token_json)
        payload["access_token"] = f"token-{current}"
        if current == 1:
            first_request_started.set()
            assert waiter_ready.wait(timeout=2)
            time.sleep(0.05)
        return httpx.Response(200, json=payload)

    route = respx.post(TOKEN_URL).mock(side_effect=response)
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)

    def waiting_call():
        waiter_ready.set()
        return provider.get_token(NOW + timedelta(hours=2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(provider.get_token, NOW)
        assert first_request_started.wait(timeout=2)
        waiter = pool.submit(waiting_call)
        assert leader.result(timeout=3) == "token-1"
        assert waiter.result(timeout=3) == "token-2"

    assert route.call_count == 2


@respx.mock
def test_mixed_now_waiters_share_only_tokens_valid_for_each_caller(
    client, token_json
):
    first_request_started = threading.Event()
    all_waiters_ready = threading.Event()
    waiter_count = 0
    waiter_lock = threading.Lock()
    post_count = 0
    post_lock = threading.Lock()

    def response(_request):
        nonlocal post_count
        with post_lock:
            post_count += 1
            current = post_count
        payload = copy.deepcopy(token_json)
        payload["access_token"] = f"token-{current}"
        if current == 1:
            first_request_started.set()
            assert all_waiters_ready.wait(timeout=2)
            time.sleep(0.05)
        return httpx.Response(200, json=payload)

    route = respx.post(TOKEN_URL).mock(side_effect=response)
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)

    def waiting_call(caller_now):
        nonlocal waiter_count
        with waiter_lock:
            waiter_count += 1
            if waiter_count == 8:
                all_waiters_ready.set()
        return provider.get_token(caller_now)

    valid_times = [NOW + timedelta(minutes=5)] * 4
    stale_times = [NOW + timedelta(hours=2)] * 4
    with ThreadPoolExecutor(max_workers=9) as pool:
        leader = pool.submit(provider.get_token, NOW)
        assert first_request_started.wait(timeout=2)
        futures = [
            pool.submit(waiting_call, caller_now)
            for caller_now in valid_times + stale_times
        ]
        assert leader.result(timeout=3) == "token-1"
        results = [future.result(timeout=3) for future in futures]

    assert results[:4] == ["token-1"] * 4
    assert results[4:] == ["token-2"] * 4
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("status", "category", "posts_per_generation"),
    [
        (403, FailureCategory.AUTH, 1),
        (500, FailureCategory.NETWORK, 3),
    ],
)
@respx.mock
def test_concurrent_failed_refresh_is_single_flight_per_generation(
    client, status, category, posts_per_generation
):
    first_post = threading.Event()
    post_count = 0
    post_lock = threading.Lock()

    def response(_request):
        nonlocal post_count
        with post_lock:
            post_count += 1
            current = post_count
        if current == 1:
            first_post.set()
            time.sleep(0.1)
        return httpx.Response(status, text="private credential response")

    route = respx.post(TOKEN_URL).mock(side_effect=response)
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)
    callers_ready = threading.Barrier(8)

    def invoke():
        callers_ready.wait()
        try:
            provider.get_token(NOW)
        except ConnectorFailure as error:
            return error
        raise AssertionError("failed token acquisition unexpectedly succeeded")

    with ThreadPoolExecutor(max_workers=8) as pool:
        errors = list(pool.map(lambda _index: invoke(), range(8)))

    assert first_post.is_set()
    assert route.call_count == posts_per_generation
    assert all(error.category is category for error in errors)
    assert all(error.retryable is (category is FailureCategory.NETWORK) for error in errors)
    assert all("private" not in f"{error!s} {error!r}" for error in errors)

    with pytest.raises(ConnectorFailure) as later:
        provider.get_token(NOW + timedelta(seconds=1))
    assert later.value.category is category
    assert route.call_count == posts_per_generation * 2


@respx.mock
def test_invalidation_can_target_only_rejected_cached_token(token_provider, token_json):
    second = copy.deepcopy(token_json)
    second["access_token"] = "fresh-token"
    route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(200, json=token_json), httpx.Response(200, json=second)]
    )
    assert token_provider.get_token(NOW) == "access-token"
    token_provider.invalidate("different-token")
    assert token_provider.get_token(NOW + timedelta(seconds=1)) == "access-token"
    token_provider.invalidate("access-token")
    assert token_provider.get_token(NOW + timedelta(seconds=2)) == "fresh-token"
    token_provider.invalidate()
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("payload", []),
        ("access_token", ""),
        ("access_token", "token\nheader"),
        ("access_token", "token\x00header"),
        ("access_token", "x" * 4097),
        ("token_type", "Basic"),
        ("expires_in", True),
        ("expires_in", 0),
        ("expires_in", "3600"),
        ("expires_in", 1.5),
        ("expires_in", 31_536_001),
        ("expires_in", 10**100),
        ("scope", "read:inventory"),
        ("scope", ["read:events"]),
    ],
)
@respx.mock
def test_malformed_token_payload_is_safe_nonretryable_parse(
    token_provider, token_json, mutation, value
):
    payload = value if mutation == "payload" else copy.deepcopy(token_json)
    if mutation != "payload":
        payload[mutation] = value
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=payload, text=None)
    )
    with pytest.raises(ConnectorFailure) as caught:
        token_provider.get_token(NOW)
    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.PARSE,
        False,
    )
    assert route.call_count == 1
    exposed = f"{caught.value!s} {caught.value!r} {caught.value.args!r}"
    assert CLIENT_ID not in exposed
    assert CLIENT_SECRET not in exposed
    assert "access-token" not in exposed


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
def test_token_http_failures_are_mapped_and_retried_safely(
    client, status, category, retryable, calls
):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(status, text="private access-token credentials")
    )
    sleeps = []
    provider = StubHubTokenProvider(settings(), client, sleep=sleeps.append)
    with pytest.raises(ConnectorFailure) as caught:
        provider.get_token(NOW)
    assert (caught.value.category, caught.value.retryable) == (category, retryable)
    assert route.call_count == calls
    assert len(sleeps) == calls - 1
    exposed = f"{caught.value!s} {caught.value!r}"
    assert "private" not in exposed
    assert CLIENT_SECRET not in exposed


@respx.mock
def test_token_network_failure_is_retryable_and_hides_raw_exception(client):
    request = httpx.Request("POST", TOKEN_URL, headers={"private": CLIENT_SECRET})
    route = respx.post(TOKEN_URL).mock(
        side_effect=httpx.TimeoutException(
            f"private timeout {CLIENT_SECRET}", request=request
        )
    )
    provider = StubHubTokenProvider(settings(), client, sleep=lambda _delay: None)
    with pytest.raises(ConnectorFailure) as caught:
        provider.get_token(NOW)
    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.NETWORK,
        True,
    )
    assert route.call_count == 3
    assert CLIENT_SECRET not in f"{caught.value!s} {caught.value!r}"


@respx.mock
def test_search_uses_exact_catalog_endpoint_bearer_and_parameters(
    connector, token_json, search_json
):
    authorize(token_json)
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search_json))

    connector.discover(
        Team.TEXANS,
        datetime(2026, 9, 20, 6, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 4, tzinfo=timezone.utc),
    )

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer access-token"
    assert dict(request.url.params) == {
        "q": "Houston Texans",
        "page_size": "100",
        "country_code": "US",
        "exclude_parking_passes": "true",
        "dateLocal": "2026-09-20",
    }
    assert request.url.copy_with(query=None) == httpx.URL(SEARCH_URL)
    assert "access-token" not in str(request.url)


@respx.mock
def test_multiday_window_omits_date_local_and_aggies_uses_exact_query(
    connector, token_json, search_json
):
    authorize(token_json)
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search_json))
    connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)
    params = dict(route.calls[0].request.url.params)
    assert params["q"] == "Texas A&M Aggies Football"
    assert "dateLocal" not in params


@respx.mock
def test_maps_both_supported_home_teams_and_filters_away_parking_status(
    connector, token_json, search_json
):
    authorize(token_json)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search_json))

    texans = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    aggies = connector.discover(Team.AGGIES, WINDOW_START, WINDOW_END)

    assert [(item.external_id, item.team, item.opponent, item.venue) for item in texans] == [
        ("120001", Team.TEXANS, "Baltimore Ravens", "NRG Stadium")
    ]
    assert [(item.external_id, item.team, item.opponent, item.venue) for item in aggies] == [
        ("120002", Team.AGGIES, "LSU Tigers", "Kyle Field")
    ]
    assert all(item.is_home and not item.is_parking for item in texans + aggies)


@pytest.mark.parametrize(
    "mutation",
    [
        "neutral",
        "parking_name",
        "tailgate_category",
        "deleted",
        "draft",
        "contingent",
        "unconfirmed",
        "wrong_team_category",
        "mismatched_opponent",
    ],
)
@respx.mock
def test_rejects_nonhome_product_inactive_or_unreconciled_events(
    connector, token_json, search_json, mutation
):
    event = detail(search_json)
    if mutation == "neutral":
        event["venue"]["name"] = "AT&T Stadium"
    elif mutation == "parking_name":
        event["name"] += " ParkingPass"
    elif mutation == "tailgate_category":
        event["categories"].append({"name": "VIP Tailgating"})
    elif mutation in {"deleted", "draft", "contingent"}:
        event["status"] = mutation.title()
    elif mutation == "unconfirmed":
        event["date_confirmed"] = False
    elif mutation == "wrong_team_category":
        event["categories"] = [{"name": "Baltimore Ravens"}]
    else:
        event["categories"][1]["name"] = "Buffalo Bills"
    authorize(token_json)
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [event]}})
    )
    assert connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END) == []


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"_embedded": []}, {"_embedded": {}}, {"_embedded": {"events": {}}}],
)
@respx.mock
def test_search_requires_explicit_hal_events_list(connector, token_json, payload):
    authorize(token_json)
    response = httpx.Response(200, content=b"not-json") if payload is None else httpx.Response(200, json=payload)
    respx.get(SEARCH_URL).mock(return_value=response)
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.PARSE,
        False,
    )


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("https://stubhub.com/event/1?token=private#fragment", "https://stubhub.com/event/1"),
        ("https://www.stubhub.com/event/1", "https://www.stubhub.com/event/1"),
        ("http://stubhub.com/event/1", None),
        ("https://evil.example/event/1", None),
        ("https://stubhub.com.evil.example/event/1", None),
        ("https://user@stubhub.com/event/1", None),
        ("https://stubhub.com:444/event/1", None),
        ("https://stubhub.com/event/\x00hidden", None),
        ("https://stubhub.com/event/line\nbreak", None),
        ("https://stubhub.com/event/%00hidden", None),
        ("https://stubhub.com/event/%0Abreak", None),
        ("https://stubhub.com/event/%7Fdelete", None),
        ("https://stubhub.com/event/%GG", None),
        ("https://stubhub.com/event/%", None),
        (
            "https://stubhub.com/event/valid%20seat?tracking=%00#fragment%0A",
            "https://stubhub.com/event/valid%20seat",
        ),
    ],
)
@respx.mock
def test_public_url_uses_only_sanitized_event_webpage_link(
    connector, token_json, search_json, raw_url, expected
):
    event = detail(search_json)
    event["_links"]["event:webpage"]["href"] = raw_url
    event["_links"]["self"]["href"] = "https://api.stubhub.net/private/query?token=x"
    authorize(token_json)
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [event]}})
    )
    assert connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)[0].url == expected


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (WINDOW_START.replace(tzinfo=None), WINDOW_END),
        (WINDOW_START, WINDOW_END.replace(tzinfo=None)),
        (WINDOW_START, WINDOW_START),
        (WINDOW_END, WINDOW_START),
    ],
)
def test_discovery_rejects_naive_or_unordered_window(connector, start, end):
    with pytest.raises(ValueError, match="time window"):
        connector.discover(Team.TEXANS, start, end)


@respx.mock
def test_search_dedupes_sorts_and_rejects_out_of_window(
    connector, token_json, search_json
):
    base = detail(search_json)
    earlier = copy.deepcopy(base)
    earlier.update(id=120010, name="Buffalo Bills at Houston Texans")
    earlier["categories"][1]["name"] = "Buffalo Bills"
    earlier["start_date"] = "2026-09-10T12:00:00-05:00"
    outside = copy.deepcopy(base)
    outside.update(id=120011, start_date="2026-12-01T12:00:00-06:00")
    authorize(token_json)
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"_embedded": {"events": [base, earlier, earlier, outside]}},
        )
    )
    events = connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert [item.external_id for item in events] == ["120010", "120001"]


@respx.mock
def test_401_invalidates_rejected_token_and_refreshes_exactly_once(
    connector, token_json, search_json
):
    first = copy.deepcopy(token_json)
    second = copy.deepcopy(token_json)
    first["access_token"] = "rejected-token"
    second["access_token"] = "fresh-token"
    token_route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    search_route = respx.get(SEARCH_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=search_json)]
    )
    connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert token_route.call_count == 2
    assert search_route.call_count == 2
    assert [call.request.headers["authorization"] for call in search_route.calls] == [
        "Bearer rejected-token",
        "Bearer fresh-token",
    ]


@pytest.mark.parametrize(("status", "expected_catalog_calls"), [(401, 2), (403, 1)])
@respx.mock
def test_auth_failure_never_refreshes_in_a_loop(
    connector, token_json, status, expected_catalog_calls
):
    token_route = authorize(token_json)
    catalog_route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert (caught.value.category, caught.value.retryable) == (FailureCategory.AUTH, False)
    assert catalog_route.call_count == expected_catalog_calls
    assert token_route.call_count == (2 if status == 401 else 1)


@pytest.mark.parametrize(
    ("status", "category", "retryable", "calls"),
    [
        (429, FailureCategory.RATE_LIMIT, True, 3),
        (500, FailureCategory.NETWORK, True, 3),
        (400, FailureCategory.PARSE, False, 1),
    ],
)
@respx.mock
def test_catalog_failures_are_mapped_and_retried_safely(
    connector, token_json, status, category, retryable, calls
):
    authorize(token_json)
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(status, text="private body access-token raw-id-999")
    )
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert (caught.value.category, caught.value.retryable) == (category, retryable)
    assert route.call_count == calls
    exposed = f"{caught.value!s} {caught.value!r}"
    assert "access-token" not in exposed
    assert "private" not in exposed
    assert "raw-id-999" not in exposed


@respx.mock
def test_catalog_network_failure_retries_without_exposing_request(connector, token_json):
    authorize(token_json)
    request = httpx.Request(
        "GET",
        f"{SEARCH_URL}?private=raw-id-999",
        headers={"authorization": "Bearer access-token"},
    )
    route = respx.get(SEARCH_URL).mock(
        side_effect=httpx.TimeoutException(
            "private body access-token raw-id-999", request=request
        )
    )
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.NETWORK,
        True,
    )
    assert route.call_count == 3
    exposed = f"{caught.value!s} {caught.value!r}"
    assert "access-token" not in exposed
    assert "private" not in exposed
    assert "raw-id-999" not in exposed


@respx.mock
def test_catalog_minimum_is_event_floor_not_listing(
    connector, token_json, search_json, stubhub_event
):
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=detail(search_json)))
    observation = connector.fetch_observations(stubhub_event)[0]
    assert observation.kind is ObservationKind.EVENT_FLOOR
    assert observation.pair_price == Decimal("246.91")
    assert observation.currency == "USD"
    assert observation.observed_at == OBSERVED_AT
    assert observation.can_buy_pair is None
    assert observation.listing_id is None
    assert observation.section is None
    assert observation.row is None
    assert observation.quantity_available is None
    assert observation.buyer_fees is None
    assert observation.estimated_tax is None


@respx.mock
def test_detail_accepts_merged_replacement_but_preserves_requested_identity(
    connector, token_json, search_json, stubhub_event
):
    payload = detail(search_json)
    payload["id"] = 120099
    payload["name"] = "Baltimore Ravens Football at Houston Texans"
    payload["categories"][1]["name"] = "Baltimore Ravens Football"
    payload["start_date"] = "2026-09-20T12:04:00-05:00"
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    observation = connector.fetch_observations(stubhub_event)[0]
    assert observation.event_external_id == "120001"


@pytest.mark.parametrize(
    "mutation",
    ["different_opponent", "kickoff_outside_tolerance", "different_date"],
)
@respx.mock
def test_detail_rejects_replacement_for_another_game(
    connector, token_json, search_json, stubhub_event, mutation
):
    payload = detail(search_json)
    payload["id"] = 120099
    if mutation == "different_opponent":
        payload["name"] = "Buffalo Bills at Houston Texans"
        payload["categories"][1]["name"] = "Buffalo Bills"
    elif mutation == "kickoff_outside_tolerance":
        payload["start_date"] = "2026-09-20T12:06:00-05:00"
    else:
        payload["start_date"] = "2026-09-27T12:00:00-05:00"
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(stubhub_event)

    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.PARSE,
        False,
    )
    assert "120099" not in f"{caught.value!s} {caught.value!r}"


@respx.mock
def test_detail_missing_webpage_link_reuses_only_sanitized_discovered_url(
    connector, token_json, search_json, stubhub_event
):
    payload = detail(search_json)
    payload["_links"].pop("event:webpage")
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    observation = connector.fetch_observations(stubhub_event)[0]
    assert observation.listing_url == stubhub_event.url

    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    unsafe_event = replace(
        stubhub_event, url="https://attacker@stubhub.com/event/120001?token=private"
    )
    observation = connector.fetch_observations(unsafe_event)[0]
    assert observation.listing_url is None


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (None, []),
        ({"amount": "0.001", "currency_code": "USD"}, []),
        ({"amount": "10.005", "currency_code": "USD"}, [Decimal("20.01")]),
    ],
)
@respx.mock
def test_missing_or_zero_minimum_yields_no_floor(
    connector, token_json, search_json, stubhub_event, price, expected
):
    payload = detail(search_json)
    if price is None:
        payload.pop("min_ticket_price")
    else:
        payload["min_ticket_price"] = price
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    observations = connector.fetch_observations(stubhub_event)
    assert [item.pair_price for item in observations] == expected


@pytest.mark.parametrize(
    "price",
    [
        [],
        {"amount": True, "currency_code": "USD"},
        {"amount": "NaN", "currency_code": "USD"},
        {"amount": "Infinity", "currency_code": "USD"},
        {"amount": "-0.01", "currency_code": "USD"},
        {"amount": "10", "currency_code": "CAD"},
    ],
)
@respx.mock
def test_invalid_minimum_is_safe_parse(
    connector, token_json, search_json, stubhub_event, price
):
    payload = detail(search_json)
    payload["min_ticket_price"] = price
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(stubhub_event)
    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.PARSE,
        False,
    )


@pytest.mark.parametrize("event_id", ["", "0", "-1", "2147483648", "12/34", "secret-id"])
@respx.mock
def test_detail_rejects_unsafe_int32_id_without_network(connector, stubhub_event, event_id):
    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(replace(stubhub_event, external_id=event_id))
    assert caught.value.category is FailureCategory.PARSE
    assert "secret-id" not in f"{caught.value!s} {caught.value!r}"
    assert respx.calls.call_count == 0


@respx.mock
def test_detail_revalidates_team_home_and_venue(
    connector, token_json, search_json, stubhub_event
):
    payload = detail(search_json)
    payload["venue"]["name"] = "AT&T Stadium"
    authorize(token_json)
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ConnectorFailure) as caught:
        connector.fetch_observations(stubhub_event)
    assert caught.value.category is FailureCategory.PARSE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("id", 2_147_483_648),
        ("start_date", "2026-09-20T12:00:00"),
        ("start_date", "not-a-date private"),
    ],
)
@respx.mock
def test_relevant_search_event_requires_safe_id_and_aware_date(
    connector, token_json, search_json, field, value
):
    payload = detail(search_json)
    payload[field] = value
    authorize(token_json)
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"_embedded": {"events": [payload]}})
    )
    with pytest.raises(ConnectorFailure) as caught:
        connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    assert (caught.value.category, caught.value.retryable) == (
        FailureCategory.PARSE,
        False,
    )
    assert "private" not in f"{caught.value!s} {caught.value!r}"


def test_settings_replace_obsolete_key_with_bounded_oauth_timeout():
    value = settings()
    assert value.stubhub_client_id.get_secret_value() == CLIENT_ID
    assert value.stubhub_client_secret.get_secret_value() == CLIENT_SECRET
    assert value.stubhub_http_timeout_seconds == 10.0
    assert not hasattr(value, "stubhub_api_key")
    with pytest.raises(ValidationError):
        settings(stubhub_http_timeout_seconds=0)
    with pytest.raises(ValidationError):
        settings(stubhub_http_timeout_seconds=121)


def test_bootstrap_rejects_partial_oauth_before_acquiring_client(monkeypatch):
    clients = []
    monkeypatch.setattr(
        "ticket_reviewer.bootstrap.httpx.Client", lambda **kwargs: clients.append(kwargs)
    )
    for client_id, client_secret in ((CLIENT_ID, ""), ("", CLIENT_SECRET)):
        with pytest.raises(ValueError, match="StubHub OAuth credentials must be configured together"):
            build_services(
                Settings(
                    _env_file=None,
                    stubhub_client_id=client_id,
                    stubhub_client_secret=client_secret,
                ),
                session_factory=lambda: None,
            )
    assert clients == []


def test_bootstrap_registers_exactly_one_owned_stubhub_and_preserves_injected():
    services = build_services(settings(), session_factory=lambda: None)
    assert [item.source for item in services.connectors] == [Source.STUBHUB]
    owned = services.connectors[0]
    assert CLIENT_ID not in repr(services)
    assert CLIENT_SECRET not in repr(services)
    assert owned.client.is_closed is False
    services.close()
    services.close()
    assert owned.client.is_closed is True

    class Injected:
        source = Source.STUBHUB
        capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})

        def discover(self, *_args):
            return []

        def fetch_observations(self, _event):
            return []

        def close(self):
            raise AssertionError("injected connector must not be closed")

    injected = Injected()
    services = build_services(settings(), connectors=(injected,), session_factory=lambda: None)
    assert services.connectors == (injected,)
    services.close()


def test_three_owned_connectors_are_all_attempted_on_close(monkeypatch):
    services = build_services(
        settings(ticketmaster_api_key="tm-key", seatgeek_client_id="sg-id"),
        session_factory=lambda: None,
    )
    assert [item.source for item in services.connectors] == [
        Source.TICKETMASTER,
        Source.SEATGEEK,
        Source.STUBHUB,
    ]
    first, second, third = services.connectors
    original_first_close = first.close

    def fail_first_close():
        original_first_close()
        raise RuntimeError("sanitized close failure")

    monkeypatch.setattr(first, "close", fail_first_close)
    with pytest.raises(RuntimeError, match="sanitized close failure"):
        services.close()
    assert first.client.is_closed and second.client.is_closed and third.client.is_closed
    services.close()


def test_scanner_failure_preserves_original_and_attempts_all_three_closes(monkeypatch):
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

    original_close = __import__(
        "ticket_reviewer.connectors.ticketmaster", fromlist=["TicketmasterConnector"]
    ).TicketmasterConnector.close

    def failing_first_close(self):
        original_close(self)
        raise RuntimeError("sanitized cleanup failure")

    monkeypatch.setattr("ticket_reviewer.bootstrap.httpx.Client", client_factory)
    monkeypatch.setattr(
        "ticket_reviewer.bootstrap.TicketmasterConnector.close", failing_first_close
    )
    monkeypatch.setattr(
        "ticket_reviewer.bootstrap.ScanCoordinator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("sanitized scanner construction failure")
        ),
    )
    with pytest.raises(ValueError, match="scanner construction failure") as caught:
        build_services(
            settings(ticketmaster_api_key="tm-key", seatgeek_client_id="sg-id"),
            session_factory=lambda: None,
        )
    assert [client.close_calls for client in clients] == [1, 1, 1]
    assert any("cleanup failed" in note for note in getattr(caught.value, "__notes__", ()))


def test_import_does_not_acquire_an_http_client():
    probe = (
        "import httpx; "
        "httpx.Client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('acquired')); "
        "import ticket_reviewer.bootstrap"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


@respx.mock
def test_runtime_endpoint_surface_is_oauth_and_catalog_only(
    connector, token_json, search_json, stubhub_event
):
    authorize(token_json)
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search_json))
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=detail(search_json)))
    connector.discover(Team.TEXANS, WINDOW_START, WINDOW_END)
    connector.fetch_observations(stubhub_event)
    assert {
        (call.request.method, call.request.url.host, call.request.url.path)
        for call in respx.calls
    } == {
        ("POST", "account.stubhub.com", "/oauth2/token"),
        ("GET", "api.stubhub.net", "/catalog/events/search"),
        ("GET", "api.stubhub.net", "/catalog/events/120001"),
    }
