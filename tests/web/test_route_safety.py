import socket
from types import SimpleNamespace
import urllib.request

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ticket_reviewer.config import Settings
from ticket_reviewer.data.schema import Base, OpportunityRow, OutcomeRow, SettingRow
from ticket_reviewer.main import create_app
from ticket_reviewer.web import routes

from .conftest import NOW, PassiveScheduler, seed_opportunity


class TrackingSession(Session):
    closes = 0
    commits = 0

    def close(self):
        TrackingSession.closes += 1
        return super().close()

    def commit(self):
        TrackingSession.commits += 1
        return super().commit()


def tracking_client(*, raise_server_exceptions=True):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine, class_=TrackingSession, expire_on_commit=False
    )
    services = SimpleNamespace(
        session_factory=factory,
        connectors=(),
        scanner=SimpleNamespace(run=lambda _now: None),
        close=lambda: None,
    )
    app = create_app(
        Settings(_env_file=None),
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: services,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
        clock=lambda: NOW,
    )
    return engine, factory, TestClient(
        app, raise_server_exceptions=raise_server_exceptions
    )


def test_get_routes_close_sessions_without_network_commit_or_mutation(monkeypatch):
    engine, factory, client = tracking_client()
    try:
        with factory() as session:
            event, _observation, opportunity = seed_opportunity(session, profit=82)
            Session.commit(session)
        TrackingSession.closes = 0
        TrackingSession.commits = 0

        def fail_network(*_args, **_kwargs):
            raise AssertionError("dashboard GET attempted network access")

        monkeypatch.setattr(socket, "create_connection", fail_network)
        monkeypatch.setattr(urllib.request, "urlopen", fail_network)
        with client:
            before = client.get("/")
            settings = client.get("/settings")
            event_detail = client.get(f"/events/{event.id}")
            missing = client.get("/events/999999")
            invalid = client.get("/?team=")
        with factory() as session:
            count = session.scalar(select(OpportunityRow).count()) if False else len(
                session.scalars(select(OpportunityRow)).all()
            )
            status = session.get(OpportunityRow, opportunity.id).status
            outcomes = session.scalars(select(OutcomeRow)).all()
            persisted_settings = session.scalars(select(SettingRow)).all()
        assert before.status_code == 200
        assert settings.status_code == 200
        assert event_detail.status_code == 200
        assert missing.status_code == 404
        assert invalid.status_code == 400
        assert TrackingSession.commits == 0
        assert TrackingSession.closes >= 5
        assert count == 1
        assert status == "new"
        assert outcomes == []
        assert persisted_settings == []
    finally:
        engine.dispose()


def test_session_closes_when_template_rendering_fails(monkeypatch):
    engine, _, client = tracking_client(raise_server_exceptions=False)
    original = routes.templates.TemplateResponse

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("private template path")

    monkeypatch.setattr(routes.templates, "TemplateResponse", fail_render)
    TrackingSession.closes = 0
    try:
        with client:
            response = client.get("/")
        assert response.status_code == 500
        assert "private template path" not in response.text
        assert TrackingSession.closes == 1
    finally:
        monkeypatch.setattr(routes.templates, "TemplateResponse", original)
        engine.dispose()
