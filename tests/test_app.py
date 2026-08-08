from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ticket_reviewer.config import Settings
from ticket_reviewer.main import create_app, run_migrations


NOW = datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc)


class RecordingServices:
    def __init__(self, order, *, close_error=None):
        self.order = order
        self.close_error = close_error
        self.scanner = SimpleNamespace(run=self.run_scan)

    def run_scan(self, now):
        self.order.append(("scan", now))

    def close(self):
        self.order.append("services.close")
        if self.close_error is not None:
            raise self.close_error


class RecordingScheduler:
    def __init__(self, scan, order, *, start_error=None, shutdown_error=None):
        self.scan = scan
        self.order = order
        self.start_error = start_error
        self.shutdown_error = shutdown_error

    def start(self):
        self.order.append("scheduler.start")
        if self.start_error is not None:
            raise self.start_error
        self.scan(NOW)

    def shutdown(self, *, wait):
        self.order.append(("scheduler.shutdown", wait))
        if self.shutdown_error is not None:
            raise self.shutdown_error


def injected_app(order, *, service=None, scheduler=None):
    settings = Settings(_env_file=None, database_url="sqlite:///private-location.db")
    services = service or RecordingServices(order)

    def migrate(database_url):
        order.append(("migrate", database_url))

    def build_services(received_settings):
        order.append(("services.build", received_settings is settings))
        return services

    def build_scheduler(scan, received_settings, *, clock):
        order.append(
            (
                "scheduler.build",
                received_settings is settings,
                clock(),
            )
        )
        return scheduler or RecordingScheduler(scan, order)

    return create_app(
        settings,
        migration_runner=migrate,
        services_factory=build_services,
        scheduler_factory=build_scheduler,
        clock=lambda: NOW,
    )


def test_health_route_does_not_expose_secrets():
    order = []
    settings = Settings(
        _env_file=None,
        ticketmaster_api_key="secret",
        database_url="sqlite:///private-location.db",
    )
    app = create_app(
        settings,
        migration_runner=lambda _url: order.append("migrate"),
        services_factory=lambda _settings: RecordingServices(order),
        scheduler_factory=lambda scan, _settings, *, clock: RecordingScheduler(
            scan, order
        ),
        clock=lambda: NOW,
    )

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dry_run": True}
    assert "secret" not in response.text
    assert "private-location" not in response.text


def test_lifespan_orders_migration_services_scheduler_immediate_scan_and_cleanup():
    order = []
    app = injected_app(order)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "dry_run": True}
        assert app.state.services.scanner is not None
        assert app.state.scheduler is not None

    assert order == [
        ("migrate", "sqlite:///private-location.db"),
        ("services.build", True),
        ("scheduler.build", True, NOW),
        "scheduler.start",
        ("scan", NOW),
        ("scheduler.shutdown", False),
        "services.close",
    ]


def test_migration_failure_acquires_no_services_or_scheduler():
    order = []
    failure = RuntimeError("private migration detail")

    def migrate(_url):
        order.append("migrate")
        raise failure

    app = create_app(
        Settings(_env_file=None),
        migration_runner=migrate,
        services_factory=lambda _settings: order.append("services"),
        scheduler_factory=lambda *_args, **_kwargs: order.append("scheduler"),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError) as caught:
        with TestClient(app):
            pass

    assert caught.value is failure
    assert order == ["migrate"]


def test_service_construction_failure_builds_no_scheduler_and_preserves_error():
    order = []
    failure = RuntimeError("private service detail")

    def build_services(_settings):
        order.append("services")
        raise failure

    app = create_app(
        Settings(_env_file=None),
        migration_runner=lambda _url: order.append("migrate"),
        services_factory=build_services,
        scheduler_factory=lambda *_args, **_kwargs: order.append("scheduler"),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError) as caught:
        with TestClient(app):
            pass

    assert caught.value is failure
    assert order == ["migrate", "services"]


def test_scheduler_construction_failure_closes_services_without_masking_error():
    order = []
    failure = RuntimeError("private scheduler detail")
    services = RecordingServices(order)

    def build_scheduler(*_args, **_kwargs):
        order.append("scheduler.build")
        raise failure

    app = create_app(
        Settings(_env_file=None),
        migration_runner=lambda _url: order.append("migrate"),
        services_factory=lambda _settings: (order.append("services.build"), services)[1],
        scheduler_factory=build_scheduler,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError) as caught:
        with TestClient(app):
            pass

    assert caught.value is failure
    assert order == ["migrate", "services.build", "scheduler.build", "services.close"]


def test_scheduler_start_failure_attempts_nonwaiting_shutdown_then_closes_services():
    order = []
    failure = RuntimeError("private start detail")
    services = RecordingServices(order)
    scheduler = RecordingScheduler(services.scanner.run, order, start_error=failure)
    app = create_app(
        Settings(_env_file=None),
        migration_runner=lambda _url: order.append("migrate"),
        services_factory=lambda _settings: (order.append("services.build"), services)[1],
        scheduler_factory=lambda *_args, **_kwargs: (
            order.append("scheduler.build"),
            scheduler,
        )[1],
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError) as caught:
        with TestClient(app):
            pass

    assert caught.value is failure
    assert order == [
        "migrate",
        "services.build",
        "scheduler.build",
        "scheduler.start",
        ("scheduler.shutdown", False),
        "services.close",
    ]


def test_shutdown_attempts_both_cleanups_and_preserves_first_generic_failure_note():
    order = []
    scheduler_failure = RuntimeError("private scheduler shutdown detail")
    services_failure = RuntimeError("private services close detail")
    services = RecordingServices(order, close_error=services_failure)
    scheduler = RecordingScheduler(
        services.scanner.run,
        order,
        shutdown_error=scheduler_failure,
    )
    app = create_app(
        Settings(_env_file=None),
        migration_runner=lambda _url: order.append("migrate"),
        services_factory=lambda _settings: (order.append("services.build"), services)[1],
        scheduler_factory=lambda *_args, **_kwargs: (
            order.append("scheduler.build"),
            scheduler,
        )[1],
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError) as caught:
        with TestClient(app):
            pass

    assert caught.value is scheduler_failure
    assert caught.value.__notes__ == ["additional application cleanup failed"]
    assert "private services close detail" not in " ".join(caught.value.__notes__)
    assert order[-2:] == [("scheduler.shutdown", False), "services.close"]


def test_default_services_release_their_owned_database_engine_on_shutdown(tmp_path):
    database_path = tmp_path / "lifespan.db"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{database_path}",
    )

    class PassiveScheduler:
        def start(self):
            pass

        def shutdown(self, *, wait):
            assert wait is False

    app = create_app(
        settings,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    database_path.unlink()
    assert not database_path.exists()


def test_migration_runner_creates_a_missing_sqlite_parent_directory(tmp_path):
    database_path = tmp_path / "missing" / "nested" / "application.db"
    assert not database_path.parent.exists()

    run_migrations(f"sqlite:///{database_path}")

    assert database_path.is_file()
