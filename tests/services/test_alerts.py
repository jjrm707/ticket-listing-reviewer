from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
from threading import Barrier, Lock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from tests.factories import make_estimate, make_event, make_observation
from ticket_reviewer.bootstrap import build_services
from ticket_reviewer.config import RuntimeSettings, Settings
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.data.repositories import (
    AlertRepository,
    EventRepository,
    ObservationRepository,
    OpportunityRepository,
    OutcomeRepository,
)
from ticket_reviewer.data.schema import (
    AlertRow,
    Base,
    ObservationRow,
    OpportunityRow,
    OutcomeRow,
)
from ticket_reviewer.domain.enums import (
    Confidence,
    ObservationKind,
    OpportunityStatus,
    Source,
    Team,
)
from ticket_reviewer.services.alerts import (
    AlertCandidate,
    AlertPolicy,
    AlertService,
    NtfyPublisher,
    NotificationFailure,
    PreviousAlert,
    PushMessage,
    sanitize_click_url,
)
from ticket_reviewer.services.scanner import RepositoryBundle


NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)
TOPIC = "Task10AlertTopic_7K3mP9vR2xQ8wL5z"
TOKEN = "Token_7K3mP9vR2xQ8wL5z"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "ntfy" / "success.json"


def runtime(**overrides):
    values = {
        "budget_cap": Decimal("400.00"),
        "alert_profit_threshold": Decimal("50.00"),
        "profit_improvement_threshold": Decimal("20.00"),
        "observation_freshness_minutes": 120,
        "scan_interval_minutes": 60,
        "ticketmaster_seller_fee_rate": Decimal("0.15"),
        "seatgeek_seller_fee_rate": Decimal("0.15"),
        "stubhub_seller_fee_rate": Decimal("0.15"),
    }
    values.update(overrides)
    return RuntimeSettings(**values)


def candidate(**overrides):
    values = {
        "source": Source.STUBHUB,
        "event_external_id": "texans-colts-2026",
        "listing_identity": "listing:pair-123",
        "observed_at": NOW,
        "team": Team.TEXANS,
        "opponent": "Colts",
        "acquisition_total": Decimal("240.00"),
        "estimated_net_profit": Decimal("63.00"),
        "roi": Decimal("0.263"),
        "confidence": Confidence.MEDIUM,
        "actionable": True,
        "status": OpportunityStatus.NEW,
        "kind": ObservationKind.LISTING,
        "currency": "USD",
        "pair_price": Decimal("240.00"),
        "section": "123",
        "row": "G",
        "quantity_available": 2,
        "can_buy_pair": True,
        "listing_url": "https://www.stubhub.com/event/123?tracking=private#fragment",
    }
    values.update(overrides)
    return AlertCandidate(**values)


@pytest.fixture
def policy():
    return AlertPolicy(runtime())


def test_new_qualifying_opportunity_sends(policy):
    decision = policy.decide(candidate(), previous_alert=None, stale=False)

    assert decision.should_send is True
    assert decision.reason == "qualifying opportunity"
    assert len(decision.fingerprint) == 64


def test_49_99_does_not_send(policy):
    decision = policy.decide(
        candidate(estimated_net_profit=Decimal("49.99")), None, False
    )

    assert decision.should_send is False
    assert decision.reason == "below profit threshold"


def test_policy_rejects_forged_runtime_thresholds_below_global_floors():
    forged = RuntimeSettings.model_construct(
        **{
            **runtime().model_dump(),
            "alert_profit_threshold": Decimal("1.00"),
            "profit_improvement_threshold": Decimal("0.00"),
        }
    )

    with pytest.raises(TypeError):
        AlertPolicy(forged)


def test_repeat_requires_20_dollar_improvement(policy):
    prior = PreviousAlert(profit_at_send=Decimal("50.00"), fingerprint="a" * 64)
    unchanged = candidate(estimated_net_profit=Decimal("69.99"))
    improved = candidate(estimated_net_profit=Decimal("70.00"))

    assert policy.decide(unchanged, prior, False).should_send is False
    assert policy.decide(improved, prior, False).should_send is True


def test_stale_opportunity_never_sends(policy):
    assert policy.decide(candidate(), previous_alert=None, stale=True).should_send is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"actionable": False}, "not actionable"),
        ({"actionable": 1}, "not actionable"),
        ({"estimated_net_profit": Decimal("NaN")}, "invalid opportunity data"),
        ({"acquisition_total": Decimal("NaN")}, "invalid opportunity data"),
        ({"acquisition_total": Decimal("0")}, "outside budget"),
        ({"acquisition_total": Decimal("400.01")}, "outside budget"),
        ({"confidence": Confidence.LOW}, "low confidence"),
        ({"status": OpportunityStatus.PASSED}, "ineligible status"),
        ({"status": OpportunityStatus.PURCHASED}, "ineligible status"),
        ({"status": OpportunityStatus.SOLD}, "ineligible status"),
        ({"status": OpportunityStatus.EXPIRED}, "ineligible status"),
        ({"kind": ObservationKind.EVENT_FLOOR}, "not a confirmed listing"),
        ({"kind": ObservationKind.EVENT_AGGREGATE}, "not a confirmed listing"),
        ({"can_buy_pair": False}, "not a confirmed listing"),
        ({"can_buy_pair": 1}, "not a confirmed listing"),
        ({"quantity_available": True}, "not a confirmed listing"),
        ({"quantity_available": 1}, "not a confirmed listing"),
        ({"currency": "EUR"}, "not a confirmed listing"),
        ({"pair_price": Decimal("0")}, "not a confirmed listing"),
        ({"pair_price": Decimal("NaN")}, "invalid opportunity data"),
        ({"listing_identity": ""}, "invalid opportunity data"),
    ],
)
def test_each_rejection_gate_fails_closed(policy, changes, reason):
    decision = policy.decide(candidate(**changes), None, False)

    assert decision.should_send is False
    assert decision.reason == reason


def test_synthetic_missing_listing_identity_is_not_a_confirmed_listing(policy):
    decision = policy.decide(candidate(listing_identity="missing:listing"), None, False)

    assert decision.should_send is False
    assert decision.reason == "not a confirmed listing"


def test_extreme_stored_profit_fails_closed_instead_of_breaking_fingerprinting(policy):
    decision = policy.decide(
        candidate(estimated_net_profit=Decimal("1e999999")), None, False
    )

    assert decision.should_send is False
    assert decision.reason == "invalid opportunity data"


@pytest.mark.parametrize(
    "status", [OpportunityStatus.NEW, OpportunityStatus.WATCHING]
)
def test_new_and_watching_are_alert_eligible(policy, status):
    assert policy.decide(candidate(status=status), None, False).should_send is True


def test_exact_fingerprint_is_unchanged_across_restart(policy):
    first = policy.decide(candidate(), None, False)
    prior = PreviousAlert(
        profit_at_send=Decimal("43.00"), fingerprint=first.fingerprint
    )

    second = AlertPolicy(runtime()).decide(candidate(), prior, False)

    assert second.should_send is False
    assert second.reason == "already sent"


def test_fingerprint_uses_collision_resistant_canonical_encoding(policy):
    left = policy.decide(
        candidate(
            event_external_id="a|listing:b", listing_identity="listing:c"
        ),
        None,
        False,
    )
    right = policy.decide(
        candidate(
            event_external_id="a", listing_identity="listing:b|listing:c"
        ),
        None,
        False,
    )

    assert left.fingerprint != right.fingerprint
    assert left.fingerprint.isascii()
    assert left.fingerprint == left.fingerprint.lower()


def test_fingerprint_cent_normalizes_money_and_utc_timestamp(policy):
    offset = timezone(timedelta(hours=-5))
    left = policy.decide(
        candidate(
            acquisition_total=Decimal("240.001"),
            estimated_net_profit=Decimal("63.001"),
        ),
        None,
        False,
    )
    right = policy.decide(
        candidate(
            acquisition_total=Decimal("240.00"),
            estimated_net_profit=Decimal("63.00"),
            observed_at=NOW.astimezone(offset),
        ),
        None,
        False,
    )

    assert left.fingerprint == right.fingerprint


def test_message_copy_is_safe_bounded_and_omits_private_fields(policy):
    secret = "private-token-value"
    message = policy.message(
        candidate(
            opponent="Colts\r\nInjected: yes",
            section="123\nInjected: yes",
            row="G\x00secret",
            listing_url=f"https://stubhub.com/event/123?token={secret}",
        )
    )

    assert message.title == "Texans vs Colts Injected yes - Pair opportunity"
    assert message.body == (
        "Section 123 Injected yes, Row G secret\n"
        "Buy: $240.00 all-in | Est. net: $63.00 | ROI: 26.3%\n"
        "Confidence: medium"
    )
    assert message.click_url == "https://stubhub.com/event/123"
    assert "\r" not in repr(message)
    assert secret not in repr(message)
    assert len(message.title) <= 160
    assert len(message.body) <= 1024


def test_message_omits_unknown_seat_and_roi_without_rendering_none(policy):
    message = policy.message(candidate(section=None, row=None, roi=None))

    assert message.body == (
        "Buy: $240.00 all-in | Est. net: $63.00\nConfidence: medium"
    )
    assert "None" not in message.body


@pytest.mark.parametrize(
    ("source", "url", "expected"),
    [
        (Source.STUBHUB, "https://stubhub.com/event/1?q=x#f", "https://stubhub.com/event/1"),
        (Source.SEATGEEK, "https://www.seatgeek.com/events/1", "https://www.seatgeek.com/events/1"),
        (Source.TICKETMASTER, "https://ticketmaster.com/event/1", "https://ticketmaster.com/event/1"),
        (Source.STUBHUB, "http://stubhub.com/event/1", None),
        (Source.STUBHUB, "https://user@stubhub.com/event/1", None),
        (Source.STUBHUB, "https://stubhub.com:444/event/1", None),
        (Source.STUBHUB, "https://api.stubhub.net/event/1", None),
        (Source.STUBHUB, "https://127.0.0.1/event/1", None),
        (Source.STUBHUB, "https://seatgeek.com/event/1", None),
        (Source.STUBHUB, "https://stubhub.com/event/%0Ainject", None),
        (Source.STUBHUB, "https://stubhub.com/event/café", None),
        (Source.MANUAL, "https://stubhub.com/event/1", None),
    ],
)
def test_click_url_requires_matching_public_https_host(source, url, expected):
    assert sanitize_click_url(source, url) == expected


def test_freshness_boundary_is_inclusive_with_only_five_minute_future_skew():
    policy = AlertPolicy(runtime(observation_freshness_minutes=120))

    assert policy.is_stale(NOW - timedelta(minutes=120), NOW) is False
    assert policy.is_stale(NOW + timedelta(minutes=5), NOW) is False
    assert policy.is_stale(NOW - timedelta(minutes=120, microseconds=1), NOW) is True
    assert policy.is_stale(NOW + timedelta(minutes=5, microseconds=1), NOW) is True
    assert policy.is_stale(NOW, NOW.replace(tzinfo=None)) is True


def test_push_message_rejects_header_injection_and_malformed_unicode():
    with pytest.raises(ValueError, match="invalid push message"):
        PushMessage("bad\r\ntitle", "body", "high", ("tickets",), None)
    with pytest.raises(ValueError, match="invalid push message"):
        PushMessage("bad\ud800", "body", "high", ("tickets",), None)


def test_push_message_rejects_an_unsanitized_click_target():
    with pytest.raises(ValueError, match="invalid push message"):
        PushMessage(
            "Title",
            "Body",
            "high",
            ("ticket",),
            "https://evil.example/private?token=secret",
        )


def _success_response(request):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return httpx.Response(200, json=payload, request=request)


def test_ntfy_publisher_posts_exact_safe_request_and_returns_id():
    requests = []

    def handler(request):
        requests.append(request)
        return _success_response(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = NtfyPublisher(TOPIC, TOKEN, 7.5, client=client)
    message = PushMessage(
        "Texans vs Colts - Pair opportunity",
        "Buy: $240.00 all-in",
        "high",
        ("ticket", "moneybag"),
        "https://stubhub.com/event/1",
    )

    provider_id = publisher.publish(message)

    assert provider_id == "provider-message-123"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://ntfy.sh/{TOPIC}"
    assert request.content == b"Buy: $240.00 all-in"
    assert request.headers["Title"] == "Texans vs Colts - Pair opportunity"
    assert request.headers["Priority"] == "high"
    assert request.headers["Tags"] == "ticket,moneybag"
    assert request.headers["Click"] == "https://stubhub.com/event/1"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "topic",
    ["", "short", "../secret", "topic/segment", "topic?query", "topic#frag", "has space", "line\nbreak", "a" * 129],
)
def test_topic_is_rejected_before_request_client_acquisition(monkeypatch, topic):
    acquired = False

    def acquire(*args, **kwargs):
        nonlocal acquired
        acquired = True
        raise AssertionError("client must not be acquired")

    monkeypatch.setattr(httpx, "Client", acquire)

    with pytest.raises(ValueError, match="invalid notification configuration"):
        NtfyPublisher(topic, None, 10)

    assert acquired is False


@pytest.mark.parametrize("token", ["", " leading", "trailing ", "line\nbreak", "a" * 2049])
def test_optional_token_is_validated_without_exposure(token):
    with pytest.raises(ValueError, match="invalid notification configuration") as caught:
        NtfyPublisher(TOPIC, token, 10, client=object())

    if token:
        assert token not in repr(caught.value)


@pytest.mark.parametrize(
    "token",
    ["bad,token", 'bad"token', "bad\\token", "abc=def", "abc==def"],
)
def test_malformed_bearer_token_is_rejected_before_client_acquisition(
    monkeypatch, token
):
    acquired = False

    def acquire(*args, **kwargs):
        nonlocal acquired
        acquired = True
        raise AssertionError("client must not be acquired")

    monkeypatch.setattr(httpx, "Client", acquire)

    with pytest.raises(ValueError, match="invalid notification configuration"):
        NtfyPublisher(TOPIC, token, 10)

    assert acquired is False


@pytest.mark.parametrize(
    ("outcome", "retryable"),
    [
        (httpx.ReadTimeout("private timeout detail"), True),
        (httpx.ConnectError("private network detail"), True),
        (429, True),
        (503, True),
        (400, False),
        ({"event": "message"}, False),
        ({"id": ""}, False),
        ({"id": "x" * 256}, False),
        ({"id": TOPIC}, False),
        ({"id": TOKEN}, False),
    ],
)
def test_ntfy_failures_are_generic_and_never_expose_secrets(outcome, retryable):
    def handler(request):
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int):
            return httpx.Response(outcome, text=f"private {TOPIC} {TOKEN}", request=request)
        return httpx.Response(200, json=outcome, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = NtfyPublisher(TOPIC, TOKEN, 10, client=client)
    message = PushMessage("Title", "Body", "high", ("ticket",), None)

    with pytest.raises(NotificationFailure) as caught:
        publisher.publish(message)

    assert caught.value.retryable is retryable
    exposed = f"{caught.value!r} {caught.value.args}"
    assert TOPIC not in exposed
    assert TOKEN not in exposed
    assert "private" not in exposed
    assert "https://ntfy.sh" not in exposed


def test_httpx_and_httpcore_logs_redact_topic_path_and_authorization(caplog):
    publisher = NtfyPublisher(
        TOPIC,
        TOKEN,
        10,
        client=httpx.Client(transport=httpx.MockTransport(_success_response)),
    )

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpcore.http11").debug(
            "send headers Authorization=%s path=/%s", TOKEN, TOPIC
        )
        publisher.publish(PushMessage("Title", "Body", "high", ("ticket",), None))

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert TOPIC not in rendered
    assert TOKEN not in rendered
    assert "Authorization" not in rendered
    assert "https://ntfy.sh" not in rendered
    publisher.close()


def test_marketplace_http_logs_are_redacted_without_ntfy_configuration(
    database, caplog
):
    api_key = "LogSentinelTicketmasterKey_7K3mP9vR2xQ8wL5z"
    auth_header = "LogSentinelStubHubBearer_8W4nQ2rT6yP1sD9k"
    services = build_services(
        Settings(_env_file=None, ticketmaster_api_key=api_key),
        session_factory=database,
        repository_factory=RepositoryBundle,
    )
    try:
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("httpx").debug(
                "GET https://app.ticketmaster.com/?apikey=%s Authorization=Bearer %s",
                api_key,
                auth_header,
            )
            logging.getLogger("httpcore.http11").debug(
                "send_request_headers Authorization=Bearer %s", auth_header
            )
            logging.getLogger("httpcore.socks").debug(
                "setup_socks5_connection apikey=%s Authorization=Bearer %s",
                api_key,
                auth_header,
            )
    finally:
        services.close()

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert api_key not in rendered
    assert auth_header not in rendered
    assert "Authorization" not in rendered


def test_ntfy_fixture_contains_only_obvious_placeholders():
    fixture = FIXTURE.read_text(encoding="utf-8")

    assert "placeholder-topic" in fixture
    assert TOPIC not in fixture
    assert TOKEN not in fixture
    assert "authorization" not in fixture.casefold()
    assert "cookie" not in fixture.casefold()
    assert "account" not in fixture.casefold()


def test_injected_publisher_client_is_reused_and_never_closed():
    class Client:
        def __init__(self):
            self.closed = 0

        def post(self, url, **kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(200, json={"id": "message-1"}, request=request)

        def close(self):
            self.closed += 1

    client = Client()
    publisher = NtfyPublisher(TOPIC, None, 10, client=client)
    message = PushMessage("Title", "Body", "high", ("ticket",), None)

    assert publisher.publish(message) == "message-1"
    assert publisher.publish(message) == "message-1"
    publisher.close()
    publisher.close()

    assert client.closed == 0


@pytest.fixture
def database(tmp_path):
    engine, factory = create_engine_and_session(f"sqlite:///{tmp_path / 'alerts.db'}")
    Base.metadata.create_all(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def save_opportunity(
    factory,
    *,
    observed_at=NOW,
    listing_id="listing-123",
    source=Source.STUBHUB,
    event_external_id="event-123",
    profit=Decimal("63.00"),
    acquisition=Decimal("240.00"),
    actionable=True,
    confidence=Confidence.MEDIUM,
):
    event = make_event(
        source=source,
        external_id=event_external_id,
        opponent="Colts",
        starts_at=NOW + timedelta(days=30),
        url="https://stubhub.com/event/123",
    )
    observation = make_observation(
        source=source,
        event_external_id=event_external_id,
        observed_at=observed_at,
        pair_price=acquisition,
        buyer_fees=Decimal("0"),
        estimated_tax=Decimal("0"),
        listing_id=listing_id,
        listing_url="https://stubhub.com/event/123?private=value",
    )
    estimate = make_estimate(
        acquisition_total=acquisition,
        projected_resale_gross=acquisition + profit,
        projected_proceeds=acquisition + profit,
        estimated_net_profit=profit,
        roi=profit / acquisition,
        actionable=actionable,
        confidence=confidence,
    )
    with factory() as session:
        event_id = EventRepository(session).upsert(event)
        observation_id = ObservationRepository(session).add(event_id, observation)
        opportunity_id = OpportunityRepository(session).save_estimate(
            event_id, observation_id, estimate
        )
        session.commit()
    return opportunity_id


class RecordingPublisher:
    def __init__(self, outcome="provider-123"):
        self.outcome = outcome
        self.messages = []

    def publish(self, message):
        self.messages.append(message)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_dry_run_records_success_without_http_and_deduplicates_after_restart(database):
    opportunity_id = save_opportunity(database)
    settings = Settings(_env_file=None, dry_run=True)
    forbidden = RecordingPublisher(AssertionError("dry-run must not publish"))

    first = AlertService(settings, database, publisher=forbidden).evaluate_and_send(
        opportunity_id, NOW
    )
    second = AlertService(settings, database, publisher=forbidden).evaluate_and_send(
        opportunity_id, NOW
    )

    assert first.should_send is True
    assert second.should_send is False
    assert second.reason == "already sent"
    assert forbidden.messages == []
    with database() as session:
        rows = session.scalars(select(AlertRow)).all()
        assert len(rows) == 1
        assert rows[0].provider_message_id == "dry-run"
        assert rows[0].fingerprint == first.fingerprint


def test_explicit_static_notification_test_is_allowed_while_dry_run(database):
    publisher = RecordingPublisher("test-provider")
    service = AlertService(
        Settings(_env_file=None, dry_run=True), database, publisher=publisher
    )

    result = service.test_notification()

    assert result.code == "sent"
    assert len(publisher.messages) == 1
    assert "TEST ONLY" in publisher.messages[0].title
    assert "TEST ONLY" in publisher.messages[0].body
    with database() as session:
        assert session.scalars(select(AlertRow)).all() == []


def test_delivery_reservation_is_committed_before_live_publication(database):
    opportunity_id = save_opportunity(database)

    class InspectingPublisher:
        def publish(self, _message):
            with database() as session:
                row = session.scalar(select(AlertRow))
                assert row.delivery_state == "pending"
                assert row.sent_at is None
                assert row.provider_message_id is None
            return "provider-reserved"

    decision = AlertService(
        Settings(_env_file=None, dry_run=False),
        database,
        publisher=InspectingPublisher(),
        clock=lambda: NOW + timedelta(minutes=7),
    ).evaluate_and_send(opportunity_id, NOW)

    assert decision.should_send is True
    with database() as session:
        row = session.scalar(select(AlertRow))
        assert row.delivery_state == "sent"
        assert row.reserved_at == NOW + timedelta(minutes=7)
        assert row.sent_at == NOW + timedelta(minutes=7)


def test_ambiguous_crash_reservation_suppresses_restart_duplicate(database):
    opportunity_id = save_opportunity(database)

    class CrashAfterAcceptance:
        def __init__(self):
            self.calls = 0

        def publish(self, _message):
            self.calls += 1
            raise SystemExit("simulated process exit after provider acceptance")

    crashing = CrashAfterAcceptance()
    with pytest.raises(SystemExit):
        AlertService(
            Settings(_env_file=None, dry_run=False),
            database,
            publisher=crashing,
        ).evaluate_and_send(opportunity_id, NOW)

    restarted = RecordingPublisher("must-not-send")
    result = AlertService(
        Settings(_env_file=None, dry_run=False),
        database,
        publisher=restarted,
    ).evaluate_and_send(opportunity_id, NOW)

    assert result.should_send is False
    assert result.reason == "delivery outcome unknown"
    assert restarted.messages == []
    with database() as session:
        row = session.scalar(select(AlertRow))
        assert row.delivery_state == "pending"


def test_post_acceptance_database_failure_leaves_pending_and_never_republishes(
    database, monkeypatch
):
    opportunity_id = save_opportunity(database)
    accepted = RecordingPublisher("provider-accepted-before-db-failure")
    original = AlertRepository.mark_sent

    def fail_mark_sent(self, alert_id, sent_at, provider_message_id):
        raise RuntimeError("simulated database failure after acceptance")

    monkeypatch.setattr(AlertRepository, "mark_sent", fail_mark_sent)
    first = AlertService(
        Settings(_env_file=None, dry_run=False),
        database,
        publisher=accepted,
    ).evaluate_and_send(opportunity_id, NOW)
    monkeypatch.setattr(AlertRepository, "mark_sent", original)
    restarted = RecordingPublisher("must-not-duplicate")
    second = AlertService(
        Settings(_env_file=None, dry_run=False),
        database,
        publisher=restarted,
    ).evaluate_and_send(opportunity_id, NOW)

    assert first.reason == "notification temporarily unavailable"
    assert second.reason == "delivery outcome unknown"
    assert len(accepted.messages) == 1
    assert restarted.messages == []
    with database() as session:
        assert session.scalar(select(AlertRow)).delivery_state == "pending"


def test_known_transient_failure_is_retryable_from_failed_reservation(database):
    opportunity_id = save_opportunity(database)
    live = Settings(_env_file=None, dry_run=False)
    first = AlertService(
        live,
        database,
        publisher=RecordingPublisher(NotificationFailure(retryable=True)),
    ).evaluate_and_send(opportunity_id, NOW)
    retry_publisher = RecordingPublisher("provider-retry")
    second = AlertService(
        live, database, publisher=retry_publisher
    ).evaluate_and_send(opportunity_id, NOW)

    assert first.should_send is False
    assert second.should_send is True
    assert len(retry_publisher.messages) == 1


def test_service_clock_not_scan_start_controls_sent_at_and_repeat_baseline(database):
    first_id = save_opportunity(database, profit=Decimal("55.00"))
    delivery = NOW + timedelta(minutes=10)
    AlertService(
        Settings(_env_file=None, dry_run=True),
        database,
        clock=lambda: delivery,
    ).evaluate_and_send(first_id, NOW)

    with database() as session:
        row = session.scalar(select(AlertRow))
        assert row.sent_at == delivery


def test_long_scan_delivery_time_preserves_the_true_twenty_dollar_baseline(database):
    first_id = save_opportunity(database, profit=Decimal("55.00"))
    delivery = NOW + timedelta(minutes=10)
    AlertService(
        Settings(_env_file=None, dry_run=True),
        database,
        clock=lambda: delivery,
    ).evaluate_and_send(first_id, NOW)
    with database() as session:
        marker_id = save_opportunity(
            database,
            observed_at=NOW + timedelta(minutes=1),
            profit=Decimal("100.00"),
        )
        marker = OutcomeRepository(session).save(marker_id, "watching")
        session.get(OutcomeRow, marker).updated_at = NOW + timedelta(minutes=5)
        session.commit()
    later_id = save_opportunity(
        database,
        observed_at=delivery + timedelta(minutes=1),
        profit=Decimal("75.00"),
    )

    decision = AlertService(
        Settings(_env_file=None, dry_run=True),
        database,
        clock=lambda: delivery + timedelta(minutes=1),
    ).evaluate_and_send(later_id, NOW + timedelta(minutes=1))

    assert decision.should_send is True


def test_startup_reconciliation_delivers_committed_unattempted_manual_opportunity(
    database,
):
    opportunity_id = save_opportunity(
        database,
        source=Source.MANUAL,
        event_external_id="manual-review:17",
        listing_id="manual-review:17",
    )
    publisher = RecordingPublisher("provider-manual-reconcile")
    service = AlertService(
        Settings(_env_file=None, dry_run=False),
        database,
        publisher=publisher,
        clock=lambda: NOW,
    )

    decisions = service.reconcile_unattempted()

    assert len(decisions) == 1
    assert decisions[0].should_send is True
    assert len(publisher.messages) == 1
    with database() as session:
        assert session.scalar(select(AlertRow)).opportunity_id == opportunity_id


def test_live_success_persists_provider_id_and_latest_lineage_controls_improvement(database):
    first_id = save_opportunity(database, profit=Decimal("55.00"))
    first_publisher = RecordingPublisher("provider-first")
    live = Settings(_env_file=None, dry_run=False)
    assert AlertService(live, database, publisher=first_publisher).evaluate_and_send(
        first_id, NOW
    ).should_send

    second_id = save_opportunity(
        database,
        observed_at=NOW + timedelta(minutes=1),
        profit=Decimal("74.99"),
    )
    second_publisher = RecordingPublisher("provider-second")
    below = AlertService(live, database, publisher=second_publisher).evaluate_and_send(
        second_id, NOW + timedelta(minutes=1)
    )
    assert below.should_send is False
    assert second_publisher.messages == []

    third_id = save_opportunity(
        database,
        observed_at=NOW + timedelta(minutes=2),
        profit=Decimal("75.00"),
    )
    third_publisher = RecordingPublisher("provider-third")
    exact = AlertService(live, database, publisher=third_publisher).evaluate_and_send(
        third_id, NOW + timedelta(minutes=2)
    )

    assert exact.should_send is True
    with database() as session:
        assert [row.provider_message_id for row in session.scalars(select(AlertRow)).all()] == [
            "provider-first",
            "provider-third",
        ]


def test_unsuccessful_legacy_alert_row_does_not_block_a_new_delivery(database):
    old_id = save_opportunity(database, profit=Decimal("100.00"))
    with database() as session:
        AlertRepository(session).record(
            old_id,
            "a" * 64,
            NOW - timedelta(minutes=1),
            Decimal("100.00"),
            None,
        )
        session.commit()
    new_id = save_opportunity(
        database,
        observed_at=NOW + timedelta(minutes=1),
        profit=Decimal("63.00"),
    )
    publisher = RecordingPublisher("provider-new")

    decision = AlertService(
        Settings(_env_file=None, dry_run=False), database, publisher=publisher
    ).evaluate_and_send(new_id, NOW + timedelta(minutes=1))

    assert decision.should_send is True
    assert len(publisher.messages) == 1


def test_exact_unsuccessful_fingerprint_is_retryable_and_becomes_successful(database):
    opportunity_id = save_opportunity(database)
    fingerprint = AlertPolicy(runtime()).decide(
        candidate(
            event_external_id="event-123",
            listing_identity="listing:listing-123",
        ),
        None,
        False,
    ).fingerprint
    with database() as session:
        AlertRepository(session).record(
            opportunity_id,
            fingerprint,
            NOW - timedelta(minutes=1),
            Decimal("63.00"),
            None,
        )
        session.commit()
    publisher = RecordingPublisher("provider-recovered")

    decision = AlertService(
        Settings(_env_file=None, dry_run=False), database, publisher=publisher
    ).evaluate_and_send(opportunity_id, NOW)

    assert decision.should_send is True
    assert len(publisher.messages) == 1
    with database() as session:
        rows = session.scalars(select(AlertRow)).all()
        assert len(rows) == 1
        assert rows[0].provider_message_id == "provider-recovered"


@pytest.mark.parametrize("retryable", [True, False])
def test_publish_failure_records_no_sent_alert_and_later_run_can_retry(database, retryable):
    opportunity_id = save_opportunity(database)
    live = Settings(_env_file=None, dry_run=False)
    failure = NotificationFailure(retryable=retryable)

    decision = AlertService(
        live, database, publisher=RecordingPublisher(failure)
    ).evaluate_and_send(opportunity_id, NOW)

    assert decision.should_send is False
    assert decision.reason == (
        "notification temporarily unavailable"
        if retryable
        else "notification rejected"
    )
    with database() as session:
        row = session.scalar(select(AlertRow))
        assert row.delivery_state == "failed"
        assert row.retryable is retryable
        assert row.sent_at is None

    retry_publisher = RecordingPublisher("provider-retry")
    retry = AlertService(
        live, database, publisher=retry_publisher
    ).evaluate_and_send(opportunity_id, NOW)
    assert retry.should_send is retryable
    assert len(retry_publisher.messages) == int(retryable)


def test_service_fails_closed_for_missing_context_bad_identity_and_bad_now(database):
    opportunity_id = save_opportunity(database)
    service = AlertService(Settings(_env_file=None), database)

    assert service.evaluate_and_send(999999, NOW).reason == "invalid opportunity context"
    assert service.evaluate_and_send(opportunity_id, NOW.replace(tzinfo=None)).reason == (
        "invalid opportunity context"
    )
    with database() as session:
        row = session.get(ObservationRow, session.get(OpportunityRow, opportunity_id).observation_id)
        row.event_external_id = "different-event"
        session.commit()
    assert service.evaluate_and_send(opportunity_id, NOW).reason == (
        "invalid opportunity context"
    )


def test_one_process_concurrency_allows_only_one_delivery_attempt(database):
    opportunity_id = save_opportunity(database)
    live = Settings(_env_file=None, dry_run=False)

    class ConcurrentPublisher:
        def __init__(self):
            self.calls = 0
            self.lock = Lock()

        def publish(self, message):
            with self.lock:
                self.calls += 1
            return "provider-concurrent"

    publisher = ConcurrentPublisher()
    service = AlertService(live, database, publisher=publisher)

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(
            pool.map(lambda _: service.evaluate_and_send(opportunity_id, NOW), range(8))
        )

    assert publisher.calls == 1
    assert sum(decision.should_send for decision in decisions) == 1
    with database() as session:
        assert len(session.scalars(select(AlertRow)).all()) == 1


@pytest.mark.parametrize("retryable", [True, False])
def test_overlapping_immediate_failures_share_one_delivery_attempt(
    database, retryable
):
    opportunity_id = save_opportunity(database)
    live = Settings(_env_file=None, dry_run=False)
    callers = 8
    start = Barrier(callers + 1)

    class ImmediateFailurePublisher:
        def __init__(self):
            self.calls = 0
            self.lock = Lock()

        def publish(self, message):
            with self.lock:
                self.calls += 1
            raise NotificationFailure(retryable=retryable)

    publisher = ImmediateFailurePublisher()
    service = AlertService(live, database, publisher=publisher)

    def invoke():
        start.wait(timeout=5)
        return service.evaluate_and_send(opportunity_id, NOW)

    with ThreadPoolExecutor(max_workers=callers) as pool:
        futures = [pool.submit(invoke) for _index in range(callers)]
        start.wait(timeout=5)
        decisions = [future.result(timeout=5) for future in futures]

    expected = (
        "notification temporarily unavailable"
        if retryable
        else "notification rejected"
    )
    assert publisher.calls == 1
    assert {decision.reason for decision in decisions} == {expected}
    later = service.evaluate_and_send(opportunity_id, NOW)
    assert later.reason == expected
    assert publisher.calls == (2 if retryable else 1)
    with database() as session:
        row = session.scalar(select(AlertRow))
        assert row.delivery_state == "failed"
        assert row.retryable is retryable


def test_ungated_immediate_failure_is_singleflight_for_overlapping_callers(database):
    opportunity_id = save_opportunity(database)

    class ImmediateFailurePublisher:
        def __init__(self):
            self.calls = 0
            self.lock = Lock()

        def publish(self, message):
            with self.lock:
                self.calls += 1
            raise NotificationFailure(retryable=True)

    publisher = ImmediateFailurePublisher()
    service = AlertService(
        Settings(_env_file=None, dry_run=False),
        database,
        publisher=publisher,
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(
            pool.map(lambda _: service.evaluate_and_send(opportunity_id, NOW), range(8))
        )

    assert publisher.calls == 1
    assert {decision.reason for decision in decisions} == {
        "notification temporarily unavailable"
    }


def test_build_services_auto_wires_owned_publisher_and_closes_it(monkeypatch, database):
    class Client:
        def __init__(self, **kwargs):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    client = Client()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    settings = Settings(_env_file=None, ntfy_topic=TOPIC)

    services = build_services(
        settings,
        session_factory=database,
        repository_factory=RepositoryBundle,
    )

    assert isinstance(services.alert_service, AlertService)
    assert services.scanner.alert_service is services.alert_service
    services.close()
    services.close()
    assert client.close_calls == 1


def test_partial_notification_configuration_fails_before_client_acquisition(
    monkeypatch, database
):
    acquired = False

    def acquire(**kwargs):
        nonlocal acquired
        acquired = True
        raise AssertionError("client must not be acquired")

    monkeypatch.setattr(httpx, "Client", acquire)
    settings = Settings(_env_file=None, ntfy_access_token=TOKEN)

    with pytest.raises(ValueError, match="invalid notification configuration"):
        build_services(settings, session_factory=database)

    assert acquired is False


def test_ntfy_timeout_setting_is_bounded_and_safe_by_default():
    assert Settings(_env_file=None).ntfy_http_timeout_seconds == 10.0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ntfy_http_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ntfy_http_timeout_seconds=121)
