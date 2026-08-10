from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ticket_reviewer.config import Settings
from ticket_reviewer.data.schema import (
    Base,
    ConnectorRunRow,
    EventRow,
    ObservationRow,
    OpportunityRow,
)
from ticket_reviewer.main import create_app


NOW = datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc)


class PassiveScheduler:
    def start(self):
        pass

    def shutdown(self, *, wait):
        assert wait is False


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, timezone="America/Chicago", dry_run=True)


@pytest.fixture
def client(session_factory, settings):
    services = SimpleNamespace(
        session_factory=session_factory,
        connectors=(),
        scanner=SimpleNamespace(run=lambda _now: None),
        close=lambda: None,
    )
    app = create_app(
        settings,
        migration_runner=lambda _url: None,
        services_factory=lambda _settings: services,
        scheduler_factory=lambda *_args, **_kwargs: PassiveScheduler(),
        clock=lambda: NOW,
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def seed_opportunity(
    session,
    *,
    profit: Decimal,
    confidence: str = "high",
    risk_reasons: list[str] | None = None,
    candidate_source: str = "stubhub",
    opponent: str = "Colts",
    section: str | None = "101",
    row: str | None = "12",
    team: str = "texans",
    status: str = "new",
    acquisition_total: Decimal = Decimal("225.00"),
) -> tuple[EventRow, ObservationRow, OpportunityRow]:
    event = EventRow(
        team=team,
        opponent=opponent,
        venue="NRG Stadium",
        starts_at=datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc),
        is_home=True,
    )
    session.add(event)
    session.flush()
    observation = ObservationRow(
        event_id=event.id,
        source=candidate_source,
        event_external_id=f"event-{event.id}",
        observed_at=NOW,
        kind="listing",
        currency="USD",
        pair_price=Decimal("200.00"),
        buyer_fees=Decimal("20.00"),
        estimated_tax=Decimal("5.00"),
        section=section,
        row=row,
        quantity_available=2,
        can_buy_pair=True,
        listing_id=f"listing-{event.id}",
        listing_identity=f"listing:listing-{event.id}",
        listing_url="https://www.stubhub.com/example?secret=removed#fragment",
        freshness_at=NOW,
    )
    session.add(observation)
    session.flush()
    opportunity = OpportunityRow(
        event_id=event.id,
        observation_id=observation.id,
        acquisition_total=acquisition_total,
        exit_source="stubhub",
        projected_resale_gross=Decimal("400.00"),
        seller_fee_rate=Decimal("0.15"),
        projected_proceeds=Decimal("340.00"),
        estimated_net_profit=profit,
        roi=profit / acquisition_total,
        confidence=confidence,
        risk_reasons=risk_reasons or [],
        actionable=True,
        status=status,
        scenarios=[
            {
                "marketplace": "stubhub",
                "projected_resale_gross": "400.00",
                "seller_fee_rate": "0.15",
                "projected_proceeds": "340.00",
                "comparable_count": 3,
                "comparable_observation_ids": [],
            }
        ],
    )
    session.add(opportunity)
    session.flush()
    return event, observation, opportunity


@pytest.fixture
def seeded_opportunities(session_factory):
    with session_factory() as session:
        seed_opportunity(session, profit=Decimal("51.00"), opponent="Jaguars")
        seed_opportunity(session, profit=Decimal("82.00"), opponent="Colts")
        session.commit()


@pytest.fixture
def low_confidence_opportunity(session_factory):
    with session_factory() as session:
        seed_opportunity(
            session,
            profit=Decimal("67.00"),
            confidence="low",
            risk_reasons=["fewer than 3 seat-level comparables"],
        )
        session.commit()


@pytest.fixture
def failed_run(session_factory):
    with session_factory() as session:
        session.add(
            ConnectorRunRow(
                source="stubhub",
                started_at=NOW,
                finished_at=NOW,
                success=False,
                observation_count=0,
                redacted_error="authentication failed: client_secret=[REDACTED]",
            )
        )
        session.commit()
