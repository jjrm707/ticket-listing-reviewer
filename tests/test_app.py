from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ticket_reviewer.config import Settings
from ticket_reviewer.main import create_app, run_migrations
from ticket_reviewer.services.instance_guard import FileInstanceGuard


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
        instance_guard_factory=lambda _settings: SimpleNamespace(
            acquire=lambda: None, release=lambda: None
        ),
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

    response = TestClient(app, base_url="http://127.0.0.1").get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dry_run": True}
    assert "secret" not in response.text
    assert "private-location" not in response.text


def test_app_factory_accepts_local_ocr_engine_without_invoking_it():
    order = []
    ocr_engine = SimpleNamespace(
        extract_text=lambda _path: pytest.fail("OCR must not run during construction")
    )

    app = create_app(
        Settings(_env_file=None),
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: RecordingServices(order),
        scheduler_factory=lambda scan, _settings, *, clock: RecordingScheduler(scan, order),
        clock=lambda: NOW,
        ocr_engine=ocr_engine,
    )

    assert app.state.ocr_engine is ocr_engine


def test_lifespan_orders_migration_services_scheduler_immediate_scan_and_cleanup():
    order = []
    app = injected_app(order)

    with TestClient(app, base_url="http://127.0.0.1") as client:
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
        with TestClient(app, base_url="http://127.0.0.1"):
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
        with TestClient(app, base_url="http://127.0.0.1"):
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
        with TestClient(app, base_url="http://127.0.0.1"):
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
        with TestClient(app, base_url="http://127.0.0.1"):
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
        with TestClient(app, base_url="http://127.0.0.1"):
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
        instance_guard_factory=lambda received: FileInstanceGuard.for_database_url(
            received.database_url, allowed_root=tmp_path
        ),
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200

    database_path.unlink()
    assert not database_path.exists()


def test_migration_runner_creates_a_missing_sqlite_parent_directory(tmp_path):
    database_path = tmp_path / "missing" / "nested" / "application.db"
    assert not database_path.parent.exists()

    run_migrations(f"sqlite:///{database_path}")

    assert database_path.is_file()


def test_testing_scan_route_is_explicit_header_gated_and_bounded():
    order = []
    summary = SimpleNamespace(
        sources_succeeded=(SimpleNamespace(value="stubhub"),),
        sources_failed=(),
        events_seen=1,
        observations_saved=4,
        opportunities_saved=1,
        actionable_opportunities=1,
    )
    services = RecordingServices(order)
    services.scanner = SimpleNamespace(run=lambda now: (order.append(("manual-scan", now)), summary)[1])
    app = create_app(
        Settings(_env_file=None),
        testing=True,
        test_scan_runner=services.scanner.run,
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: services,
        scheduler_factory=lambda *_args, **_kwargs: RecordingScheduler(lambda _now: None, order),
        clock=lambda: NOW,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/internal/scan").status_code == 404
        assert client.post("/internal/scan", headers={"X-Dry-Run-Test": "true"}).status_code == 404
        response = client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"})

    assert response.status_code == 200
    assert response.json() == {
        "sources_succeeded": ["stubhub"],
        "sources_failed": [],
        "events_seen": 1,
        "observations_saved": 4,
        "opportunities_saved": 1,
        "actionable_opportunities": 1,
        "purchases_attempted": 0,
    }
    assert ("manual-scan", NOW) in order


@pytest.mark.parametrize("dry_run", [False, True])
def test_testing_scan_never_falls_back_to_runtime_scanner(dry_run):
    order = []
    services = RecordingServices(order)
    services.scanner = SimpleNamespace(
        run=lambda _now: (_ for _ in ()).throw(AssertionError("runtime scanner used"))
    )
    app = create_app(
        Settings(_env_file=None, dry_run=dry_run),
        testing=True,
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: services,
        scheduler_factory=lambda *_args, **_kwargs: RecordingScheduler(
            lambda _now: None, order
        ),
        clock=lambda: NOW,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"})
    assert response.status_code == 404


def test_testing_scan_rejects_injected_runner_when_dry_run_is_disabled():
    calls = []
    app = create_app(
        Settings(_env_file=None, dry_run=False),
        testing=True,
        test_scan_runner=lambda now: calls.append(now),
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: RecordingServices([]),
        scheduler_factory=lambda *_args, **_kwargs: RecordingScheduler(
            lambda _now: None, []
        ),
        clock=lambda: NOW,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"})
    assert response.status_code == 404
    assert calls == []


def test_production_app_never_registers_internal_scan_route():
    app = create_app(Settings(_env_file=None))
    paths = {route.path for route in app.routes if hasattr(route, "path")}

    assert "/internal/scan" not in paths
    assert "/internal/scan" not in app.openapi()["paths"]


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "192.168.1.40:8765"},
        {"host": "0.0.0.0"},
        {"host": "localhost.example"},
        {"host": "user@localhost"},
        {"host": "localhost,evil.example"},
        {"host": "127.0.0.1:9999"},
        {"host": "127.0.0.1%0aevil.example"},
    ],
)
def test_host_header_rejects_nonloopback_or_malformed_values_before_routes(headers):
    response = TestClient(create_app(Settings(_env_file=None)), base_url="http://127.0.0.1").get(
        "/healthz", headers=headers
    )

    assert response.status_code == 400
    assert response.text == '{"detail":"Invalid local request"}'
    assert headers["host"] not in response.text


def test_duplicate_host_header_is_rejected_from_raw_asgi_headers():
    app = create_app(Settings(_env_file=None))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    import asyncio

    asyncio.run(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "scheme": "http",
                "method": "GET",
                "path": "/healthz",
                "raw_path": b"/healthz",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"127.0.0.1"), (b"host", b"localhost")],
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8765),
            },
            receive,
            send,
        )
    )

    start = next(message for message in messages if message["type"] == "http.response.start")
    assert start["status"] == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost:8765", "[::1]:8765"])
def test_loopback_host_forms_are_accepted_and_forwarding_headers_are_ignored(host):
    response = TestClient(
        create_app(Settings(_env_file=None)), base_url="http://127.0.0.1"
    ).get(
        "/healthz",
        headers={
            "host": host,
            "forwarded": "host=public.example",
            "x-forwarded-host": "192.168.1.40",
        },
    )
    assert response.status_code == 200


def test_single_instance_guard_precedes_migration_and_is_released_after_cleanup():
    order = []

    class Guard:
        def acquire(self):
            order.append("guard.acquire")

        def release(self):
            order.append("guard.release")

    app = create_app(
        Settings(_env_file=None),
        instance_guard_factory=lambda _settings: Guard(),
        migration_runner=lambda _url: order.append("migrate"),
        services_factory=lambda _settings: RecordingServices(order),
        scheduler_factory=lambda scan, _settings, *, clock: RecordingScheduler(scan, order),
        clock=lambda: NOW,
    )

    with TestClient(app, base_url="http://127.0.0.1"):
        pass

    assert order[0:2] == ["guard.acquire", "migrate"]
    assert order[-3:] == [("scheduler.shutdown", False), "services.close", "guard.release"]


def test_failed_startup_releases_instance_guard_exactly_once_before_propagating():
    order = []
    failure = RuntimeError("synthetic migration failure")

    class Guard:
        def acquire(self):
            order.append("guard.acquire")

        def release(self):
            order.append("guard.release")

    def fail_migration(_url):
        order.append("migrate")
        raise failure

    app = create_app(
        Settings(_env_file=None),
        instance_guard_factory=lambda _settings: Guard(),
        migration_runner=fail_migration,
        services_factory=lambda _settings: order.append("services"),
        scheduler_factory=lambda *_args, **_kwargs: order.append("scheduler"),
    )

    with pytest.raises(RuntimeError) as caught:
        with TestClient(app, base_url="http://127.0.0.1"):
            pass

    assert caught.value is failure
    assert order == ["guard.acquire", "migrate", "guard.release"]


def test_second_instance_fails_before_migration_services_or_scheduler(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'instance.db'}"
    first_order = []
    second_order = []
    first = create_app(
        Settings(_env_file=None, database_url=database_url),
        instance_guard_factory=lambda settings: FileInstanceGuard.for_database_url(
            settings.database_url, allowed_root=tmp_path
        ),
        migration_runner=lambda _url: first_order.append("migrate"),
        services_factory=lambda _settings: RecordingServices(first_order),
        scheduler_factory=lambda scan, _settings, *, clock: RecordingScheduler(scan, first_order),
        clock=lambda: NOW,
    )
    second = create_app(
        Settings(_env_file=None, database_url=database_url),
        instance_guard_factory=lambda settings: FileInstanceGuard.for_database_url(
            settings.database_url, allowed_root=tmp_path
        ),
        migration_runner=lambda _url: second_order.append("migrate"),
        services_factory=lambda _settings: RecordingServices(second_order),
        scheduler_factory=lambda scan, _settings, *, clock: RecordingScheduler(scan, second_order),
        clock=lambda: NOW,
    )

    with TestClient(first, base_url="http://127.0.0.1"):
        with pytest.raises(RuntimeError, match="application is already running"):
            with TestClient(second, base_url="http://127.0.0.1"):
                pass

    assert second_order == []
