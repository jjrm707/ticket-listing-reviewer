import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from threading import Barrier, Event, Lock
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ticket_reviewer.config import Settings
from ticket_reviewer.data.repositories import ALLOWED_SETTING_FIELDS, SettingRepository
from ticket_reviewer.data.schema import AlertRow, ConnectorRunRow, SettingRow
from ticket_reviewer.main import create_app
from ticket_reviewer.services.alerts import AlertService, NotificationFailure
from ticket_reviewer.web import routes

from .conftest import PassiveScheduler


def _csrf(client) -> str:
    response = client.get("/manual")
    found = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert found is not None
    return found.group(1)


def _valid_settings(token: str) -> dict[str, str]:
    return {
        "budget_cap": "400.00",
        "alert_profit_threshold": "50.00",
        "profit_improvement_threshold": "20.00",
        "observation_freshness_minutes": "120",
        "scan_interval_minutes": "60",
        "stubhub_seller_fee_rate": "0.15",
        "ticketmaster_seller_fee_rate": "0.15",
        "seatgeek_seller_fee_rate": "0.15",
        "csrf_token": token,
    }


def _assert_security_headers(response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_updates_complete_nonsecret_settings_atomically(client, session_factory, settings):
    values = _valid_settings(_csrf(client))
    values["budget_cap"] = "375.25"
    values["observation_freshness_minutes"] = "180"

    response = client.post("/settings", data=values, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?result=updated"
    _assert_security_headers(response)
    with session_factory() as session:
        effective = SettingRepository(session).effective(settings)
        assert effective.budget_cap == Decimal("375.25")
        assert effective.observation_freshness_minutes == 180
        assert {row.key for row in session.scalars(select(SettingRow))} == set(
            ALLOWED_SETTING_FIELDS
        )


def test_invalid_settings_have_no_partial_write_or_value_echo(client, session_factory):
    values = _valid_settings(_csrf(client))
    values["budget_cap"] = "350.00"
    secret = "not-money-private-value"
    values["alert_profit_threshold"] = secret

    response = client.post("/settings", data=values)

    assert response.status_code == 422
    assert secret not in response.text
    assert 'role="alert"' in response.text
    assert 'action="/settings"' in response.text
    assert "Settings were not saved" in response.text
    _assert_security_headers(response)
    with session_factory() as session:
        assert session.scalars(select(SettingRow)).all() == []


def test_settings_reject_duplicate_unknown_and_missing_csrf(client, session_factory):
    token = _csrf(client)
    duplicate = "&".join(
        [
            *(f"{key}={value}" for key, value in _valid_settings(token).items()),
            "budget_cap=300.00",
        ]
    )
    assert client.post(
        "/settings",
        content=duplicate,
        headers={"content-type": "application/x-www-form-urlencoded"},
    ).status_code == 400
    unknown = _valid_settings(token)
    unknown["ticketmaster_api_key"] = "must-not-persist"
    assert client.post("/settings", data=unknown).status_code == 400
    missing = _valid_settings(token)
    del missing["csrf_token"]
    assert client.post("/settings", data=missing).status_code == 400
    with session_factory() as session:
        assert session.scalars(select(SettingRow)).all() == []


def test_settings_rejects_oversize_urlencoded_body_before_persistence(client, session_factory):
    response = client.post(
        "/settings",
        content="budget_cap=" + ("9" * 20_000),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    with session_factory() as session:
        assert session.scalars(select(SettingRow)).all() == []


def test_settings_page_shows_only_normalized_secret_statuses(client):
    response = client.get("/settings")

    assert response.status_code == 200
    assert "Settings" in response.text
    assert "user assumptions" in response.text
    assert response.text.count('class="settings-field"') == 8
    assert "Configured" not in response.text
    assert response.text.count("Missing") >= 5
    assert "ticketmaster_api_key" not in response.text
    assert "ntfy_topic" not in response.text


def test_settings_page_exposes_only_boolean_status_for_every_secret(session_factory):
    sentinels = {
        "ticketmaster_api_key": "tm-private-sentinel-91",
        "seatgeek_client_id": "sg-id-private-sentinel-92",
        "seatgeek_client_secret": "sg-secret-private-sentinel-93",
        "stubhub_client_id": "sh-id-private-sentinel-94",
        "stubhub_client_secret": "sh-secret-private-sentinel-95",
        "ntfy_topic": "Task14PrivateTopicSentinel_96_K3mP9vR2xQ8wL5z",
        "ntfy_access_token": "private-token-sentinel-97",
    }
    settings = Settings(_env_file=None, **sentinels)
    with _live_client(session_factory, settings, alert_service=None) as custom_client:
        response = custom_client.get("/settings")

    assert response.status_code == 200
    assert response.text.count("Configured") == 5
    combined = response.text + repr(dict(response.headers))
    for secret in sentinels.values():
        assert secret not in combined


def test_health_uses_effective_freshness_override(client, session_factory):
    from datetime import timedelta

    from .conftest import NOW

    with session_factory() as session:
        SettingRepository(session).set("observation_freshness_minutes", "60")
        session.add(
            ConnectorRunRow(
                source="stubhub",
                started_at=NOW - timedelta(minutes=90),
                finished_at=NOW - timedelta(minutes=90),
                success=True,
                observation_count=1,
            )
        )
        session.commit()

    response = client.get("/health")

    assert "Stale" in response.text
    assert "90 minutes ago" in response.text


def test_dry_run_notification_test_uses_prg_without_alert_history(client, session_factory):
    response = client.post(
        "/notifications/test",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?result=dry-run"
    _assert_security_headers(response)
    page = client.get(response.headers["location"])
    assert "Dry run - no push sent" in page.text
    with session_factory() as session:
        assert session.scalars(select(AlertRow)).all() == []


def test_notification_form_failures_remain_on_accessible_settings_page(client):
    token = _csrf(client)
    requests = (
        ({"data": {}}, 400),
        ({"data": {"csrf_token": token, "unexpected": "value"}}, 400),
        (
            {
                "content": f"csrf_token={token}&csrf_token={token}",
                "headers": {"content-type": "application/x-www-form-urlencoded"},
            },
            400,
        ),
        (
            {
                "content": "csrf_token=" + ("x" * 20_000),
                "headers": {"content-type": "application/x-www-form-urlencoded"},
            },
            413,
        ),
    )

    for arguments, status_code in requests:
        response = client.post("/notifications/test", **arguments)
        assert response.status_code == status_code
        assert 'role="alert"' in response.text
        assert 'action="/notifications/test"' in response.text
        assert "Notification test was not sent" in response.text


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("budget_cap", 1.0),
        ("budget_cap", True),
        ("budget_cap", "1e2"),
        ("budget_cap", "01.00"),
        ("budget_cap", "1.001"),
        ("alert_profit_threshold", "NaN"),
        ("profit_improvement_threshold", "Infinity"),
        ("observation_freshness_minutes", "60.0"),
        ("scan_interval_minutes", 60.0),
        ("stubhub_seller_fee_rate", "0.15000"),
        ("ticketmaster_seller_fee_rate", Decimal("1E-5")),
        ("seatgeek_seller_fee_rate", "+0.15"),
        ("alert_profit_threshold", Decimal("-0.00")),
        ("profit_improvement_threshold", Decimal("-0.00")),
        ("stubhub_seller_fee_rate", Decimal("-0.0000")),
    ],
)
def test_setting_repository_rejects_unsupported_types_and_noncanonical_values(
    session_factory, key, value
):
    with session_factory() as session:
        with pytest.raises(ValueError, match="invalid value"):
            SettingRepository(session).set(key, value)
        assert session.scalars(select(SettingRow)).all() == []


def test_setting_repository_validates_complete_batch_before_mutating(session_factory):
    values = _valid_settings("unused")
    del values["csrf_token"]
    values["budget_cap"] = "350.00"
    values["alert_profit_threshold"] = "bad-private-value"

    with session_factory() as session:
        repository = SettingRepository(session)
        with pytest.raises(ValueError, match="invalid value"):
            repository.set_all(values)
        assert session.scalars(select(SettingRow)).all() == []


def _live_client(session_factory, settings, alert_service):
    services = SimpleNamespace(
        session_factory=session_factory,
        connectors=(),
        scanner=SimpleNamespace(run=lambda _now: None),
        alert_service=alert_service,
        close=lambda: None,
    )
    app = create_app(
        settings,
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: services,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
    )
    return TestClient(app, base_url="http://127.0.0.1")


def test_live_notification_route_uses_owned_alert_service_publisher_once(session_factory):
    class Publisher:
        def __init__(self):
            self.messages = []

        def publish(self, message):
            self.messages.append(message)
            return "provider-id-must-not-render"

    settings = Settings(
        _env_file=None,
        dry_run=False,
        ntfy_topic="Task14NotificationTopic_7K3mP9vR2xQ8wL5z",
    )
    publisher = Publisher()
    service = AlertService(settings, session_factory, publisher=publisher)
    with _live_client(session_factory, settings, service) as client:
        response = client.post(
            "/notifications/test",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        page = client.get(response.headers["location"])

    assert response.status_code == 303
    assert page.text.count("Test notification sent") == 1
    assert "provider-id-must-not-render" not in page.text
    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message.title == "Ticket Reviewer test"
    assert message.click_url is None
    assert "test" in message.body.casefold()


def test_notification_test_singleflights_overlapping_calls_without_history(session_factory):
    class FailingPublisher:
        def __init__(self):
            self.calls = 0
            self.entered = Event()
            self.release = Event()
            self.lock = Lock()

        def publish(self, _message):
            with self.lock:
                self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            raise NotificationFailure(retryable=True)

    settings = Settings(
        _env_file=None,
        dry_run=False,
        ntfy_topic="Task14NotificationTopic_7K3mP9vR2xQ8wL5z",
    )
    publisher = FailingPublisher()
    service = AlertService(settings, session_factory, publisher=publisher)
    start = Barrier(9)

    def invoke():
        start.wait(timeout=5)
        return service.test_notification()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(invoke) for _index in range(8)]
        start.wait(timeout=5)
        assert publisher.entered.wait(timeout=5)
        deadline = time.monotonic() + 5
        while service._active_notification_tests < 8 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert service._active_notification_tests == 8
        publisher.release.set()
        results = [future.result(timeout=5) for future in futures]

    assert publisher.calls == 1
    assert {result.code for result in results} == {"temporarily-unavailable"}
    with session_factory() as session:
        assert session.scalars(select(AlertRow)).all() == []


def test_notification_failure_result_never_exposes_exception_or_configured_secrets(
    session_factory, caplog
):
    topic = "Task14PrivateTopicFailure_7K3mP9vR2xQ8wL5z"
    token = "Task14PrivateTokenFailure_7K3mP9vR2xQ8wL5z"

    class Publisher:
        def publish(self, _message):
            raise RuntimeError(
                f"POST https://ntfy.sh/{topic} Authorization: Bearer {token} private-body"
            )

    settings = Settings(
        _env_file=None,
        dry_run=False,
        ntfy_topic=topic,
        ntfy_access_token=token,
    )
    service = AlertService(settings, session_factory, publisher=Publisher())
    with _live_client(session_factory, settings, service) as custom_client:
        response = custom_client.post(
            "/notifications/test",
            data={"csrf_token": _csrf(custom_client)},
            follow_redirects=False,
        )
        page = custom_client.get(response.headers["location"])

    combined = (
        page.text
        + repr(dict(page.headers))
        + response.headers["location"]
        + caplog.text
    )
    assert "Notification service is temporarily unavailable" in page.text
    assert topic not in combined
    assert token not in combined
    assert "private-body" not in combined


def test_overlapping_notification_posts_share_one_publish_attempt(session_factory):
    class Publisher:
        def __init__(self):
            self.calls = 0
            self.entered = Event()
            self.release = Event()
            self.lock = Lock()

        def publish(self, _message):
            with self.lock:
                self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            return "provider-test-id"

    settings = Settings(
        _env_file=None,
        dry_run=False,
        ntfy_topic="Task14ConcurrentTopic_7K3mP9vR2xQ8wL5z",
    )
    publisher = Publisher()
    service = AlertService(settings, session_factory, publisher=publisher)
    with _live_client(session_factory, settings, service) as custom_client:
        token = _csrf(custom_client)
        start = Barrier(3)

        def submit():
            start.wait(timeout=5)
            return custom_client.post(
                "/notifications/test",
                data={"csrf_token": token},
                follow_redirects=False,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _index in range(2)]
            start.wait(timeout=5)
            assert publisher.entered.wait(timeout=5)
            deadline = time.monotonic() + 1
            while service._active_notification_tests < 2 and time.monotonic() < deadline:
                time.sleep(0.001)
            publisher.release.set()
            responses = [future.result(timeout=5) for future in futures]

    assert service._active_notification_tests == 0
    assert publisher.calls == 1
    assert {response.status_code for response in responses} == {303}
    assert {response.headers["location"] for response in responses} == {
        "/settings?result=sent"
    }


def test_invalid_settings_roll_back_and_close_the_request_session(settings, session_factory):
    session = session_factory()
    calls = {"rollback": 0, "close": 0}
    original_rollback = session.rollback
    original_close = session.close

    def rollback():
        calls["rollback"] += 1
        return original_rollback()

    def close():
        calls["close"] += 1
        return original_close()

    session.rollback = rollback
    session.close = close
    services = SimpleNamespace(
        session_factory=lambda: session,
        connectors=(),
        scanner=SimpleNamespace(run=lambda _now: None),
        alert_service=None,
        close=lambda: None,
    )
    app = create_app(
        settings,
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: services,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
    )
    with TestClient(app, base_url="http://127.0.0.1") as custom_client:
        response = custom_client.post(
            "/settings",
            data={"budget_cap": "private-invalid-value"},
        )

    assert response.status_code == 400
    assert calls == {"rollback": 1, "close": 1}


def test_settings_repository_failure_renders_static_inline_error(
    client, session_factory, monkeypatch
):
    def fail(repository, _values):
        repository.session.add(SettingRow(key="budget_cap", value="275.00"))
        repository.session.flush()
        raise RuntimeError("private database detail")

    monkeypatch.setattr(routes.SettingRepository, "set_all", fail)
    response = client.post("/settings", data=_valid_settings(_csrf(client)))

    assert response.status_code == 503
    assert 'role="alert"' in response.text
    assert 'action="/settings"' in response.text
    assert "Settings could not be saved" in response.text
    assert "private database detail" not in response.text
    with session_factory() as session:
        assert session.scalars(select(SettingRow)).all() == []


def test_settings_commit_failure_rolls_back_every_override(
    client, session_factory, monkeypatch
):
    token = _csrf(client)

    def fail_commit(_session):
        raise RuntimeError("private commit detail")

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "commit", fail_commit)
        response = client.post("/settings", data=_valid_settings(token))

    assert response.status_code == 503
    assert "Settings could not be saved" in response.text
    assert "private commit detail" not in response.text
    with session_factory() as session:
        assert session.scalars(select(SettingRow)).all() == []
