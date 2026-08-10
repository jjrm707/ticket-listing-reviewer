import json
import io
import re
import socket
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from ticket_reviewer.bootstrap import build_services
from ticket_reviewer.config import Settings
from ticket_reviewer.connectors.base import Capability, ConnectorFailure, FailureCategory
from ticket_reviewer.data.db import create_engine_and_session
from ticket_reviewer.data.schema import (
    AlertRow,
    ManualReviewRow,
    ObservationRow,
    OpportunityRow,
    OutcomeRow,
)
from ticket_reviewer.domain.enums import ObservationKind, Source, Team
from ticket_reviewer.domain.models import ExternalEvent, SourceObservation
from ticket_reviewer.main import create_app
from ticket_reviewer.services.alerts import AlertService
from ticket_reviewer.services.instance_guard import FileInstanceGuard


NOW = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "e2e" / "combined_scan.json"


class SyntheticFixtureConnector:
    source = Source.STUBHUB
    capabilities = frozenset({Capability.EVENT_SEARCH, Capability.LISTING_DETAIL})

    def __init__(self, payload):
        self.payload = payload

    def discover(self, team, starts_after, starts_before):
        if team is not Team.TEXANS:
            return []
        raw = self.payload["event"]
        return [
            ExternalEvent(
                source=self.source,
                external_id=raw["external_id"],
                team=Team(raw["team"]),
                opponent=raw["opponent"],
                venue=raw["venue"],
                starts_at=datetime.fromisoformat(raw["starts_at"]),
                is_home=True,
                is_parking=False,
                url=raw["url"],
            )
        ]

    def fetch_observations(self, event):
        result = []
        for raw in self.payload["observations"]:
            result.append(
                SourceObservation(
                    source=self.source,
                    event_external_id=event.external_id,
                    observed_at=NOW,
                    kind=ObservationKind(raw["kind"]),
                    currency="USD",
                    pair_price=Decimal(raw["pair_price"]),
                    buyer_fees=Decimal(raw["buyer_fees"]) if raw["buyer_fees"] is not None else None,
                    estimated_tax=Decimal(raw["estimated_tax"]) if raw["estimated_tax"] is not None else None,
                    section=raw["section"],
                    row=raw["row"],
                    quantity_available=raw["quantity_available"],
                    can_buy_pair=raw["can_buy_pair"],
                    listing_id=raw["listing_id"],
                    listing_url="https://www.stubhub.com/synthetic-listing" if raw.get("confirmed_fixture_listing") else None,
                )
            )
        return result


class SyntheticFailingConnector:
    source = Source.SEATGEEK
    capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})

    def discover(self, team, starts_after, starts_before):
        raise ConnectorFailure(
            self.source,
            FailureCategory.NETWORK,
            "synthetic source unavailable",
            retryable=True,
        )

    def fetch_observations(self, event):
        raise AssertionError("failing synthetic connector must not fetch observations")


class SyntheticEventOnlyConnector:
    source = Source.TICKETMASTER
    capabilities = frozenset({Capability.EVENT_SEARCH, Capability.EVENT_PRICE})

    def discover(self, team, starts_after, starts_before):
        if team is not Team.TEXANS:
            return []
        return [
            ExternalEvent(
                source=self.source,
                external_id="synthetic-event-only-001",
                team=Team.TEXANS,
                opponent="Colts",
                venue="NRG Stadium",
                starts_at=NOW.replace(month=9, day=13, hour=17),
                is_home=True,
                is_parking=False,
                url="https://www.ticketmaster.com/event/synthetic-public-event",
            )
        ]

    def fetch_observations(self, event):
        return [
            SourceObservation(
                source=self.source,
                event_external_id=event.external_id,
                observed_at=NOW,
                kind=ObservationKind.EVENT_FLOOR,
                currency="USD",
                pair_price=Decimal("150.00"),
                buyer_fees=None,
                estimated_tax=None,
                section=None,
                row=None,
                quantity_available=None,
                can_buy_pair=None,
                listing_id=None,
                listing_url=event.url,
            )
        ]


class SyntheticRecordingPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)
        return "must-not-be-used"


class PassiveScheduler:
    def start(self):
        pass

    def shutdown(self, *, wait):
        assert wait is False


def _build_fixture_app(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "Synthetic test fixture only" in payload["fixture_notice"]
    database_path = tmp_path / "first-run.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{database_path}", dry_run=True)
    engine, session_factory = create_engine_and_session(settings.database_url)
    publisher = SyntheticRecordingPublisher()
    runtime = {}

    def services_factory(received):
        alerts = AlertService(received, session_factory, publisher=publisher)
        services = build_services(
            received,
            connectors=(
                SyntheticFixtureConnector(payload),
                SyntheticEventOnlyConnector(),
                SyntheticFailingConnector(),
            ),
            session_factory=session_factory,
            alert_service=alerts,
            clock=lambda: NOW,
        )
        runtime["scanner"] = services.scanner
        return services

    app = create_app(
        settings,
        testing=True,
        test_scan_runner=lambda now: runtime["scanner"].run(now),
        instance_guard_factory=lambda received: FileInstanceGuard.for_database_url(
            received.database_url, allowed_root=tmp_path
        ),
        services_factory=services_factory,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
        clock=lambda: NOW,
    )
    app.state.fake_ntfy = publisher
    return app, database_path, session_factory, engine


def test_dry_run_from_absent_database_to_dashboard_alert_and_restart(tmp_path, monkeypatch):
    network_attempts = []

    def reject_network(*args, **kwargs):
        network_attempts.append((args, kwargs))
        raise AssertionError("fixture-only dry run attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    app, database_path, session_factory, engine = _build_fixture_app(tmp_path)
    assert not database_path.exists()

    with TestClient(app, base_url="http://127.0.0.1") as client:
        scan = client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"})
        assert scan.status_code == 200
        assert scan.json()["purchases_attempted"] == 0
        assert scan.json()["sources_succeeded"] == ["stubhub", "ticketmaster"]
        assert scan.json()["sources_failed"] == ["seatgeek"]
        page = client.get("/")
        assert "$60.50" in page.text
        assert "dry-run" in page.text.casefold()
        assert "High confidence" in page.text
        assert "StubHub" in page.text
        assert "Risk reasons" in page.text
        assert app.state.fake_ntfy.messages == []
        replay = client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"})
        assert replay.json()["observations_saved"] == 0
        assert replay.json()["opportunities_saved"] == 0

        app.state.settings.screenshot_directory = tmp_path / "screenshots"
        app.state.ocr_engine = SimpleNamespace(
            extract_text=lambda _path: (
                "Houston Texans vs Colts\nSection 123 Row G\n"
                "2 tickets\n$100 each\nFees $20\nTotal $220"
            )
        )
        manual_page = client.get("/manual")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', manual_page.text)
        assert csrf is not None
        image = io.BytesIO()
        Image.new("RGB", (32, 24), "white").save(image, format="PNG")
        staged = client.post(
            "/manual/extract",
            data={
                "csrf_token": csrf.group(1),
                "reference_url": "https://www.tickpick.com/synthetic?token=e2e-private-sentinel",
            },
            files={"screenshot": ("e2e-private-sentinel.png", image.getvalue(), "image/png")},
        )
        review = re.search(r'name="review_id" value="([1-9]\d*)"', staged.text)
        assert staged.status_code == 200
        assert review is not None
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ManualReviewRow)) == 1
            assert session.scalar(
                select(func.count()).select_from(ObservationRow).where(ObservationRow.source == "manual")
            ) == 0
        confirmed = client.post(
            "/manual/confirm",
            data={
                "csrf_token": csrf.group(1),
                "review_id": review.group(1),
                "event": "Houston Texans vs Colts",
                "team": "texans",
                "opponent": "Colts",
                "marketplace": "TickPick",
                "reference_url": "https://www.tickpick.com/synthetic?token=e2e-private-sentinel",
                "kickoff": "2026-09-13T12:00:00-05:00",
                "venue": "NRG Stadium",
                "section": "123",
                "row": "G",
                "quantity": "2",
                "per_ticket_price": "100.00",
                "fees": "20.00",
                "tax": "",
                "total": "220.00",
            },
        )
        assert confirmed.status_code == 200
        with session_factory() as session:
            manual_estimate = session.scalar(
                select(OpportunityRow)
                .join(ObservationRow, OpportunityRow.observation_id == ObservationRow.id)
                .where(ObservationRow.source == "manual")
            )
            assert manual_estimate.estimated_net_profit == Decimal("60.50")
            assert manual_estimate.confidence == "medium"
            manual_opportunity_id = manual_estimate.id
            event_id = manual_estimate.event_id
        outcome = client.post(
            f"/opportunities/{manual_opportunity_id}/status",
            data={
                "csrf_token": csrf.group(1),
                "status": "watching",
                "notes": "Synthetic restart lineage",
            },
            follow_redirects=False,
        )
        assert outcome.status_code == 303
        detail = client.get(f"/events/{event_id}")
        assert "Price history by source" in detail.text
        assert "Exact comparable provenance" in detail.text
        assert "stubhub" in detail.text.casefold()
        assert "ticketmaster" in detail.text.casefold()
        assert "e2e-private-sentinel" not in page.text + detail.text
        assert app.state.fake_ntfy.messages == []

    assert database_path.is_file()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ObservationRow)) == 7
        assert session.scalar(select(func.count()).select_from(OpportunityRow)) == 2
        outcome_before = session.scalar(select(OutcomeRow))
        assert outcome_before.opportunity_id == manual_opportunity_id
        assert outcome_before.status == "watching"
        alerts = list(session.scalars(select(AlertRow).order_by(AlertRow.id)))
        assert [alert.provider_message_id for alert in alerts] == ["dry-run", "dry-run"]
        fingerprint = alerts[0].fingerprint

    restarted_runtime = {}

    def restarted_services(received):
        services = build_services(
            received,
            connectors=(
                SyntheticFixtureConnector(json.loads(FIXTURE.read_text(encoding="utf-8"))),
                SyntheticEventOnlyConnector(),
                SyntheticFailingConnector(),
            ),
            session_factory=session_factory,
            alert_service=AlertService(received, session_factory, publisher=app.state.fake_ntfy),
            clock=lambda: NOW,
        )
        restarted_runtime["scanner"] = services.scanner
        return services

    restarted = create_app(
        app.state.settings,
        testing=True,
        test_scan_runner=lambda now: restarted_runtime["scanner"].run(now),
        instance_guard_factory=lambda received: FileInstanceGuard.for_database_url(
            received.database_url, allowed_root=tmp_path
        ),
        services_factory=restarted_services,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
        clock=lambda: NOW,
    )
    with TestClient(restarted, base_url="http://127.0.0.1") as client:
        restarted_page = client.get("/")
        assert "$60.50" in restarted_page.text
        assert "e2e-private-sentinel" not in restarted_page.text
        assert client.post("/internal/scan", headers={"X-Dry-Run-Test": "1"}).json()["observations_saved"] == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ObservationRow)) == 7
        assert session.scalar(select(func.count()).select_from(OpportunityRow)) == 2
        assert session.scalar(select(func.count()).select_from(AlertRow)) == 2
        assert session.scalar(select(AlertRow.fingerprint).order_by(AlertRow.id)) == fingerprint
        outcome_after = session.scalar(select(OutcomeRow))
        assert outcome_after.opportunity_id == manual_opportunity_id
        assert outcome_after.status == "watching"
        assert outcome_after.notes == "Synthetic restart lineage"
    assert network_attempts == []
    engine.dispose()
